"""Connection assembly, outside the asset optimization surface."""
import os
from pathlib import Path
from dotenv import load_dotenv

from oak.config import Config


def load_connection(project_root,work_dir,prefix=''):
    load_dotenv(Path(project_root)/'.env')
    cfg=Config(work_dir=Path(work_dir))
    cfg.api_key=os.environ.get('ZHIPU_API_KEY','')
    if not cfg.api_key: raise RuntimeError('ZHIPU_API_KEY is not configured')
    def setting(name):
        return os.environ.get(prefix+'_'+name,'') if prefix else ''
    cfg.fast_base_url=setting('FAST_API_BASE') or os.environ.get('FAST_API_BASE','') or cfg.api_base_url
    cfg.model_fast=setting('FAST_MODEL') or os.environ.get('FAST_MODEL','') or cfg.model_fast
    cfg.fast_api_key=(setting('FAST_API_KEY') or os.environ.get('FAST_API_KEY','')) if cfg.fast_base_url!=cfg.api_base_url else cfg.api_key
    if not cfg.fast_api_key: raise RuntimeError('External fast endpoint requires its own credential')
    cfg.thinking_disabled_roles.update({'extraction','tools','bootstrap','proposal'})
    return cfg
