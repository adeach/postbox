import uvicorn

from postbox import __version__
from postbox.api import create_app
from postbox.config import load_settings

app = create_app()

if __name__ == "__main__":
    s = load_settings()
    print(f"postbox v{__version__} — instance '{s.instance}' on {s.host}:{s.port}")
    uvicorn.run(app, host=s.host, port=s.port)
