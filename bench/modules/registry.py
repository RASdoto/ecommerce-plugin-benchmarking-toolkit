"""
Module registry — maps a "plugin/resource/action" path to its module class
(ports the Node `require('./module/<path>/config.js')` dynamic load, parity P7).

Unknown paths fall back to BaseModule, which honours whatever config.json +
post.json/query.json provide (so new read/POST scenarios work without code).
"""
from __future__ import annotations

from typing import Callable, Type

from ..core.module_base import BaseModule
from . import platforms as P

REGISTRY: dict[str, Type[BaseModule]] = {
    "wp": P.WpModule,
    "fluent-cart/order/create": P.FluentCartOrderCreate,
    "fluent-cart/products/create": P.FluentCartProductsCreate,
    "fluent-cart/products/list": P.FluentCartProductsList,
    "woo/order/create": P.WooOrderCreate,
    "woo/products/create": P.WooProductsCreate,
    "woo/products/list": P.WooProductsList,
    "sure-cart/order/create": P.SureCartOrderCreate,
    "sure-cart/products/create": P.SureCartProductsCreate,
    "sure-cart/products/list": P.SureCartProductsList,
    "edd/order/create": P.EddOrderCreate,
    "edd/customer/Create": P.EddCustomerCreate,
    "edd/product/create": P.EddProductCreate,
    "edd/product/list": P.EddProductList,
}


def resolve_module_class(module_path: str) -> Type[BaseModule]:
    return REGISTRY.get(module_path.strip("/"), BaseModule)


def make_module(
    module_path: str,
    app_config: dict,
    data_dir,
    secret: Callable[[str, str | None], object] | None = None,
) -> BaseModule:
    cls = resolve_module_class(module_path)
    return cls(module_path, app_config, data_dir, secret=secret)
