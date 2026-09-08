"""Launch Art Study locally and open the browser."""
import webbrowser
import uvicorn
from app import app, PORT

if __name__ == "__main__":
    webbrowser.open(f"http://127.0.0.1:{PORT}/")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
