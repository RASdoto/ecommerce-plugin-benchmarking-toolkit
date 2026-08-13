<?php
/**
 * Plugin Name: Bench EDD API
 * Description: Registers REST create endpoints for Easy Digital Downloads used by
 *              the FluentCart/Woo/EDD performance benchmark. Creates downloads,
 *              orders (payments) and customers server-side via native EDD/WP APIs.
 *              Replaces the missing edd-cus/v1 and custom-api/v1 namespaces.
 * Version: 1.0.0
 * Author: Benchmark Suite
 *
 * Endpoints (admin-only; authenticate with a WP Application Password / Basic Auth):
 *   POST /wp-json/edd-cus/v1/create_product     body: {post_title, pricing:{amount}, count?}
 *   POST /wp-json/edd-cus/v1/create_order        body: {payment_data:{...}, count?}
 *   POST /wp-json/custom-api/v1/create_customer  body: {first_name,last_name,email,...}
 *
 * Design notes (mirrors the manual EDD Demo Creator, hardened for benchmarking):
 *   - order-create resolves a REAL existing download id server-side if the given
 *     items[].id doesn't exist, and generates a unique purchase_key.
 *   - unique emails/keys to avoid collisions across many create requests.
 *   - images intentionally omitted (irrelevant to a DB/query benchmark).
 */

if (!defined('ABSPATH')) {
    exit;
}

add_action('rest_api_init', function () {

    $perm = function () {
        return current_user_can('manage_options') || current_user_can('manage_shop_settings');
    };

    register_rest_route('edd-cus/v1', '/create_product', array(
        'methods'             => 'POST',
        'callback'            => 'bench_edd_create_product',
        'permission_callback' => $perm,
    ));

    register_rest_route('edd-cus/v1', '/create_order', array(
        'methods'             => 'POST',
        'callback'            => 'bench_edd_create_order',
        'permission_callback' => $perm,
    ));

    register_rest_route('custom-api/v1', '/create_customer', array(
        'methods'             => 'POST',
        'callback'            => 'bench_edd_create_customer',
        'permission_callback' => $perm,
    ));
});

/**
 * Create one (or `count`) EDD download(s).
 */
function bench_edd_create_product(WP_REST_Request $request) {
    $p = $request->get_json_params();
    if (!is_array($p)) { $p = $request->get_params(); }

    $count = isset($p['count']) ? max(1, (int) $p['count']) : 1;
    $title = isset($p['post_title']) ? sanitize_text_field($p['post_title']) : 'Bench Download';
    $content = isset($p['post_content']) ? wp_kses_post($p['post_content']) : 'Bench download';
    $status = isset($p['post_status']) ? sanitize_key($p['post_status']) : 'publish';
    $author = isset($p['post_author']) ? (int) $p['post_author'] : 1;

    // price: accept pricing.amount, amount, or price
    $price = 10;
    if (isset($p['pricing']['amount'])) { $price = $p['pricing']['amount']; }
    elseif (isset($p['amount'])) { $price = $p['amount']; }
    elseif (isset($p['price'])) { $price = $p['price']; }

    $ids = array();
    for ($i = 0; $i < $count; $i++) {
        $post_id = wp_insert_post(array(
            'post_title'   => $title . ' ' . uniqid(),
            'post_content' => $content,
            'post_status'  => $status,
            'post_author'  => $author,
            'post_type'    => 'download',
        ), true);
        if (is_wp_error($post_id)) {
            return new WP_REST_Response(array('status' => 'failed',
                'message' => $post_id->get_error_message()), 500);
        }
        update_post_meta($post_id, 'edd_price', $price);
        // EDD variable-price safety: mark as single-price
        update_post_meta($post_id, '_variable_pricing', 0);
        $ids[] = $post_id;
    }
    return new WP_REST_Response(array('status' => 'success', 'ids' => $ids,
        'id' => $ids[0]), 201);
}

/**
 * Create one (or `count`) EDD order/payment.
 */
function bench_edd_create_order(WP_REST_Request $request) {
    $p = $request->get_json_params();
    if (!is_array($p)) { $p = $request->get_params(); }
    $pd = isset($p['payment_data']) && is_array($p['payment_data']) ? $p['payment_data'] : $p;
    $count = isset($p['count']) ? max(1, (int) $p['count']) : 1;

    if (!function_exists('edd_insert_payment') && !function_exists('edd_add_order')) {
        return new WP_REST_Response(array('status' => 'failed',
            'message' => 'EDD not active'), 500);
    }

    // resolve a real download id (fallback to a random existing download)
    $download_id = bench_edd_resolve_download($pd);
    if (!$download_id) {
        return new WP_REST_Response(array('status' => 'failed',
            'message' => 'no downloads exist — seed products first'), 422);
    }

    $email_base = isset($pd['user_info']['email']) ? $pd['user_info']['email'] : 'bench@example.com';
    $first = isset($pd['user_info']['first_name']) ? $pd['user_info']['first_name'] : 'Bench';
    $last  = isset($pd['user_info']['last_name']) ? $pd['user_info']['last_name'] : 'Buyer';
    $currency = isset($pd['currency']) ? $pd['currency'] : 'USD';
    $status   = isset($pd['status']) ? $pd['status'] : 'complete';
    $method   = isset($pd['payment_method']) ? $pd['payment_method'] : 'manual';

    $created = array();
    for ($i = 0; $i < $count; $i++) {
        $price = isset($pd['price']) ? (float) $pd['price'] : (float) get_post_meta($download_id, 'edd_price', true);
        if ($price <= 0) { $price = 25; }
        $email = bench_edd_unique_email($email_base);

        $payment_data = array(
            'price'        => $price,
            'date'         => date('Y-m-d H:i:s'),
            'user_email'   => $email,
            'purchase_key' => strtolower(md5(uniqid('', true))),
            'currency'     => $currency,
            'user_info'    => array(
                'id' => 0, 'email' => $email,
                'first_name' => $first, 'last_name' => $last,
                'discount' => 'none',
            ),
            'downloads'    => array(array('id' => $download_id, 'options' => array())),
            'cart_details' => array(array(
                'name'       => get_the_title($download_id),
                'id'         => $download_id,
                'item_number'=> array('id' => $download_id, 'options' => array()),
                'item_price' => $price,
                'price'      => $price,
                'quantity'   => 1,
                'subtotal'   => $price,
                'tax'        => 0,
                'discount'   => 0,
                'total'      => $price,
            )),
            'status'         => $status,
            'gateway'        => $method,
            'downloads_count'=> 1,
        );

        $payment_id = 0;
        if (function_exists('edd_insert_payment')) {
            $payment_id = edd_insert_payment($payment_data);
        }
        if (!$payment_id && function_exists('edd_add_order')) {
            // EDD 3.0 fallback (minimal)
            $payment_id = edd_add_order(array(
                'status'       => ($status === 'complete') ? 'complete' : $status,
                'email'        => $email,
                'currency'     => $currency,
                'gateway'      => $method,
                'total'        => $price,
                'subtotal'     => $price,
                'purchase_key' => strtolower(md5(uniqid('', true))),
            ));
        }
        if ($payment_id) {
            if ($status === 'complete' && function_exists('edd_update_payment_status')) {
                edd_update_payment_status($payment_id, 'complete');
            }
            $created[] = $payment_id;
        }
    }

    if (empty($created)) {
        return new WP_REST_Response(array('status' => 'failed',
            'message' => 'payment insert returned no id'), 500);
    }
    return new WP_REST_Response(array('status' => 'success', 'ids' => $created,
        'payment_id' => $created[0]), 201);
}

/**
 * Create one EDD customer (+ optional WP user).
 */
function bench_edd_create_customer(WP_REST_Request $request) {
    $p = $request->get_json_params();
    if (!is_array($p)) { $p = $request->get_params(); }

    $email = isset($p['email']) ? sanitize_email($p['email']) : '';
    $email = bench_edd_unique_email($email ?: 'benchcust@example.com');
    $first = isset($p['first_name']) ? sanitize_text_field($p['first_name']) : 'Bench';
    $last  = isset($p['last_name']) ? sanitize_text_field($p['last_name']) : 'Customer';

    $user_id = 0;
    if (isset($p['user_login']) && $p['user_login']) {
        $login = sanitize_user($p['user_login'], true) . '_' . wp_generate_password(4, false);
        $pass  = isset($p['password']) && $p['password'] ? $p['password'] : wp_generate_password();
        $uid = wp_insert_user(array(
            'user_login' => $login, 'user_email' => $email, 'user_pass' => $pass,
            'first_name' => $first, 'last_name' => $last, 'role' => 'subscriber',
        ));
        if (!is_wp_error($uid)) { $user_id = $uid; }
    }

    if (!class_exists('EDD_Customer')) {
        return new WP_REST_Response(array('status' => 'failed',
            'message' => 'EDD not active'), 500);
    }
    $customer = new EDD_Customer($email);
    if (empty($customer->id)) {
        $customer->create(array(
            'email'   => $email,
            'name'    => trim($first . ' ' . $last),
            'user_id' => $user_id,
        ));
    }
    return new WP_REST_Response(array('status' => 'success',
        'customer_id' => $customer->id, 'user_id' => $user_id, 'email' => $email), 201);
}

/* ------------------------------------------------------------------ helpers */

function bench_edd_resolve_download($pd) {
    // try the id the client sent first
    if (!empty($pd['items'][0]['id'])) {
        $id = (int) $pd['items'][0]['id'];
        $post = get_post($id);
        if ($post && $post->post_type === 'download') { return $id; }
    }
    if (!empty($pd['downloads'][0]['id'])) {
        $id = (int) $pd['downloads'][0]['id'];
        $post = get_post($id);
        if ($post && $post->post_type === 'download') { return $id; }
    }
    // fall back to a random existing download
    $ids = get_posts(array('post_type' => 'download', 'posts_per_page' => 1,
        'fields' => 'ids', 'orderby' => 'rand', 'post_status' => 'publish'));
    return !empty($ids) ? (int) $ids[0] : 0;
}

function bench_edd_unique_email($email) {
    $email = is_email($email) ? $email : 'benchcust@example.com';
    if (email_exists($email)) {
        list($local, $domain) = explode('@', $email, 2);
        $email = $local . '+' . uniqid() . '@' . $domain;
    }
    return $email;
}
