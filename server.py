"""PCで見るとき用。最新データを取り込んでサイトを作り、http://localhost:8790 で開く。
（普段は GitHub Actions が毎日同じことをして GitHub Pages に公開している）"""
import functools
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import build_site
import tracker

PORT = 8790

if __name__ == "__main__":
    if "--no-update" not in sys.argv:
        try:
            tracker.update()
        except Exception as e:
            print(f"取り込みに失敗（手元のデータで表示します）: {e}")
    build_site.build()
    print(f"http://localhost:{PORT}")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(build_site.SITE))
    ThreadingHTTPServer(("127.0.0.1", PORT), handler).serve_forever()
