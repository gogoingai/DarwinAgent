"""Load a declarative asset index, never a module or a task callback."""
from pathlib import Path
import yaml

from .assets import Asset, KernelAssets


def load_assets(root: Path):
    root=Path(root).resolve()
    index=yaml.safe_load((root/'assets/index.yaml').read_text())
    if set(index)!={'assets'}: raise ValueError('Invalid asset index')
    assets=[]
    for row in index['assets']:
        if 'path' not in row: raise ValueError('Registered asset requires a path')
        path=(root/row['path']).resolve()
        if not path.is_relative_to(root/'assets') or not path.is_file(): raise ValueError('Unregistered asset path')
        assets.append(Asset(**{k:v for k,v in row.items() if k!='path'},content=path.read_text()))
    return KernelAssets(tuple(assets),{'kind':'declared_task_assets'})
