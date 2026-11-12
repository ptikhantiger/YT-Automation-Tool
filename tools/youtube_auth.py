"""One-time link between this tool and your YouTube channel.

Run it on your own computer (it opens a browser):

    python tools/youtube_auth.py                      # finds client_secret*.json
    python tools/youtube_auth.py path/to/client.json  # or name the file

What you need first (Google Cloud console, see INFO.md "Auto-upload to
YouTube"): a project with "YouTube Data API v3" enabled, the Google Auth
Platform set up (External, published "In production") and an OAuth client
of type "Desktop app", whose JSON you downloaded.

The script signs you in with Google (choose the account and channel the
videos should go to), then prints the three values the cloud workflow needs
as GitHub Actions secrets:

    YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN

It also saves them to tools/youtube_token.json (git-ignored -- never
committed) so uploads from this computer work too. Standard library only.
"""

import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
TOKEN_FILE = TOOLS / "youtube_token.json"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# upload = insert videos, set thumbnails; readonly = show which channel got linked
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def find_client_file(arg):
    if arg:
        p = Path(arg).expanduser()
        if not p.exists():
            sys.exit(f"File not found: {p}")
        return p
    places = [TOOLS, TOOLS.parent, Path.home() / "Downloads"]
    found = []
    for d in places:
        if d.exists():
            found += sorted(d.glob("client_secret*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not found:
        sys.exit("No client_secret*.json found in tools/, the project folder or Downloads.\n"
                 "Download it from Google Cloud -> Google Auth Platform -> Clients -> your Desktop client "
                 "(download icon), then run:  python tools/youtube_auth.py <that file>")
    print(f"Using {found[0]}")
    return found[0]


def load_client(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    info = data.get("installed") or data.get("web")
    if not info:
        sys.exit("That JSON isn't an OAuth client file (expected an \"installed\" section). "
                 "Create the client as type \"Desktop app\".")
    if "web" in data:
        print("Warning: this is a \"Web application\" client; a \"Desktop app\" client is what this needs. "
              "If sign-in fails with redirect_uri_mismatch, create a Desktop app client instead.")
    return info["client_id"], info["client_secret"]


class _Catcher(http.server.BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" not in q and "error" not in q:
            self.send_response(404)
            self.end_headers()
            return
        _Catcher.result = {k: v[0] for k, v in q.items()}
        ok = "code" in q
        msg = ("Linked. You can close this tab and go back to the terminal." if ok
               else f"Sign-in failed: {q.get('error', ['?'])[0]}. Check the terminal.")
        body = f"<html><body style='font:18px sans-serif;padding:40px'>{msg}</body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"Google refused the token exchange ({e.code}): {e.read().decode(errors='replace')}")


def channel_name(access_token):
    req = urllib.request.Request(
        "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
        headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            items = json.loads(r.read().decode()).get("items") or []
    except urllib.error.HTTPError as e:
        return None, f"({e.code}) {e.read().decode(errors='replace')[:300]}"
    if not items:
        return None, "this Google account has no YouTube channel"
    return items[0]["snippet"]["title"], items[0]["id"]


def main():
    client_id, client_secret = load_client(find_client_file(sys.argv[1] if len(sys.argv) > 1 else None))

    server = http.server.HTTPServer(("127.0.0.1", 0), _Catcher)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })

    print("\nOpening your browser to sign in with Google.")
    print("  - Choose the Google account that owns the channel (and the channel itself, if asked).")
    print("  - \"Google hasn't verified this app\": click Advanced -> Go to ... (unsafe). It's your own app.")
    print("  - Tick both permissions and click Continue.")
    print(f"\nIf no browser opens, paste this link into one:\n{url}\n")
    webbrowser.open(url)

    def serve():
        while not _Catcher.result:
            server.handle_request()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    t.join(timeout=600)
    server.server_close()
    res = _Catcher.result
    if not res:
        sys.exit("Timed out waiting for the sign-in (10 minutes). Run the script again.")
    if "error" in res:
        sys.exit(f"Sign-in was cancelled or refused: {res['error']}")
    if res.get("state") != state:
        sys.exit("Sign-in answer didn't match this request (state mismatch). Run the script again.")

    tok = post_form(TOKEN_URL, {
        "code": res["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    })
    refresh = tok.get("refresh_token")
    if not refresh:
        sys.exit("Google didn't return a refresh token. Remove the app at "
                 "https://myaccount.google.com/permissions and run this again.")
    granted = tok.get("scope", "")
    if SCOPES[0] not in granted:
        sys.exit("The upload permission wasn't granted (the box was unticked). Run again and tick both boxes.")

    name, cid = channel_name(tok["access_token"])
    TOKEN_FILE.write_text(json.dumps({
        "client_id": client_id, "client_secret": client_secret, "refresh_token": refresh,
        "channel": name, "channel_id": cid if name else None,
    }, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    if name:
        print(f"Linked to YouTube channel: {name}  (id {cid})")
    else:
        print(f"Signed in, but couldn't read the channel: {cid}")
    print("=" * 70)
    print("\nAdd these three as GitHub secrets:")
    print("  github.com/<you>/<repo> -> Settings -> Secrets and variables -> Actions -> New repository secret\n")
    print(f"  YT_CLIENT_ID      = {client_id}")
    print(f"  YT_CLIENT_SECRET  = {client_secret}")
    print(f"  YT_REFRESH_TOKEN  = {refresh}")
    print(f"\nAlso saved to {TOKEN_FILE} (git-ignored) for uploads from this computer.")
    print("Treat the refresh token like a password: anyone with it can upload to your channel.")
    print("To cut the link later: https://myaccount.google.com/permissions -> remove the app.")


if __name__ == "__main__":
    main()
