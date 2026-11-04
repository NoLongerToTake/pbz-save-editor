"""PBZ editor desktop entry point."""
import sys,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.dont_write_bytecode=True
sys.path[:0]=[str(ROOT),str(ROOT/'vendor')]

if __name__=='__main__':
    try:
        from pbz_editor.gui import run
        run()
    except Exception:
        details=traceback.format_exc()
        try:(ROOT/'startup_error.log').write_text(details,encoding='utf-8')
        except OSError:pass
        if sys.platform=='win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,'编辑器未能启动。详细原因已写入程序目录 startup_error.log。\n\n'+details[-1300:],'PBZ存档编辑器',0x10)
        else:print(details,file=sys.stderr)
        raise SystemExit(1)
