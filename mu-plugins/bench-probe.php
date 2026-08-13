<?php
/**
 * Plugin Name: Bench Probe
 * Description: Per-request performance profiler for the FluentCart/Woo/EDD benchmark.
 *              Emits DB query count/time, slow queries, peak memory and PHP time as
 *              response headers when a request carries the X-Bench secret header.
 *              Deployable either as an mu-plugin (drop into wp-content/mu-plugins/)
 *              OR as a normal plugin (uploaded + activated over REST).
 * Version: 1.0.0
 * Author: Benchmark Suite
 *
 * Security: only activates for requests presenting the correct X-Bench-Secret,
 * so normal site traffic is never profiled or slowed.
 */

if (!defined('WPINC')) {
    // allow loading as mu-plugin very early; bail only if clearly out of WP
    if (!defined('ABSPATH')) { return; }
}

if (!defined('BENCH_PROBE_SECRET')) {
    // The bootstrap step rewrites this constant per site with the generated secret.
    define('BENCH_PROBE_SECRET', 'REPLACE_ME_BENCH_SECRET');
}

/**
 * Read a request header in a SAPI-agnostic way.
 */
function bench_probe_header($name) {
    $key = 'HTTP_' . strtoupper(str_replace('-', '_', $name));
    if (isset($_SERVER[$key])) {
        return $_SERVER[$key];
    }
    if (function_exists('getallheaders')) {
        foreach (getallheaders() as $k => $v) {
            if (strcasecmp($k, $name) === 0) {
                return $v;
            }
        }
    }
    return null;
}

/**
 * Is this request opted in to profiling (correct secret)?
 */
function bench_probe_active() {
    static $active = null;
    if ($active !== null) {
        return $active;
    }
    $flag = bench_probe_header('X-Bench');
    if ($flag !== '1' && $flag !== 'true') {
        $active = false;
        return false;
    }
    $secret = bench_probe_header('X-Bench-Secret');
    $active = ($secret !== null && hash_equals(BENCH_PROBE_SECRET, (string) $secret));
    return $active;
}

// Turn on query saving as early as possible for profiled requests.
if (bench_probe_active() && !defined('SAVEQUERIES')) {
    define('SAVEQUERIES', true);
}

$GLOBALS['bench_probe_start'] = microtime(true);

/**
 * On shutdown, compute and emit the metrics as response headers.
 */
function bench_probe_emit() {
    if (!bench_probe_active() || headers_sent()) {
        return;
    }
    global $wpdb;

    $query_count = 0;
    $query_time  = 0.0;
    $slow        = 0;
    $slow_threshold = 0.05; // 50 ms

    if (isset($wpdb) && is_array($wpdb->queries)) {
        $query_count = count($wpdb->queries);
        foreach ($wpdb->queries as $q) {
            // $q = [ sql, duration_seconds, caller ]
            $dur = isset($q[1]) ? (float) $q[1] : 0.0;
            $query_time += $dur;
            if ($dur >= $slow_threshold) {
                $slow++;
            }
        }
    }

    $php_time_ms = (microtime(true) - $GLOBALS['bench_probe_start']) * 1000.0;
    $peak_mb     = memory_get_peak_usage(true) / (1024 * 1024);

    header('Server-Timing: app;dur=' . round($php_time_ms, 2));
    header('X-Bench-Query-Count: ' . $query_count);
    header('X-Bench-Query-Time: ' . round($query_time * 1000.0, 2)); // ms
    header('X-Bench-Slow-Queries: ' . $slow);
    header('X-Bench-Peak-Memory: ' . round($peak_mb, 2));           // MB
    header('X-Bench-PHP-Time: ' . round($php_time_ms, 2));          // ms

    // Optional: dump normalized query list for EXPLAIN / N+1 analysis.
    if (bench_probe_header('X-Bench-Dump') === '1' && isset($wpdb) && is_array($wpdb->queries)) {
        $dir = defined('WP_CONTENT_DIR') ? WP_CONTENT_DIR : sys_get_temp_dir();
        $file = rtrim($dir, '/') . '/bench-probe-queries.log';
        $lines = array();
        foreach ($wpdb->queries as $q) {
            $sql = isset($q[0]) ? preg_replace('/\s+/', ' ', trim($q[0])) : '';
            $lines[] = round((isset($q[1]) ? $q[1] : 0) * 1000, 2) . "ms\t" . $sql;
        }
        @file_put_contents($file, implode("\n", $lines) . "\n", FILE_APPEND);
    }
}
register_shutdown_function('bench_probe_emit');
