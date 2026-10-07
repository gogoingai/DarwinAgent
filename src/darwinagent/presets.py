"""Explicit historical benchmark routing; generic configuration never enables it."""


def legacy_benchmark_config(config_type=None):
    from .config import Config

    return (config_type or Config)(
        api_base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        model_strong="glm-5.3",
        model_fast="glm-5.3-flash",
        model_middle="MiniMax-M3.1-Flash-Preview",
        model_profiles=True,
    )
