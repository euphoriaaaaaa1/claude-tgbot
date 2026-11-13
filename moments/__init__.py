"""moments 包的共用小工具。"""
import functools
import importlib.util
import os

_INSTALL_HOOK_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "scripts", "install_compact_hook.py")


@functools.lru_cache(maxsize=1)
def _quote_rule():
    """取 scripts/install_compact_hook.py 的 quote_hook_path：路径引号规则只此一份，这里按文件路径加载复用。
    scripts/ 不是包，脚本又是独立入口，所以不走 import 语句；首次用到时才加载，加载失败照常抛出。"""
    spec = importlib.util.spec_from_file_location("_moments_install_compact_hook", _INSTALL_HOOK_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.quote_hook_path


def shell_path(path: str, windows: bool = (os.name == "nt")) -> str:
    """发给 bot 照抄执行的命令里的一段路径（解释器或脚本）。规则同 hook command：Windows 一律双引号
    （Git Bash 下保住反斜杠、空格不切段）；POSIX 用 shlex.quote，安全字符集内原样不变。"""
    return _quote_rule()(path, windows)
