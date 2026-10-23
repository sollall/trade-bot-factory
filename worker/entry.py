"""
Cloudflare Python Worker の入口。
JS の世界 (D1 / fetch / Request / Response) と tbf.worker.app の間を橋渡しするだけの薄い層。
scripts/build_worker.py が .build/ にコピーし、bot_registry.py を生成する。
"""

import json

from js import JSON, Object
from js import fetch as js_fetch
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint

from bot_registry import BOTS
from tbf.worker.app import App, Settings


class D1:
    """tbf.worker.db.DB を D1 binding で実装"""

    def __init__(self, binding):
        self.binding = binding

    def _stmt(self, sql, params):
        # Python の None は JS の undefined になり D1 が拒否するため、SQL の NULL に置き換える
        if any(p is None for p in params):
            pieces = sql.split("?")
            if len(pieces) - 1 != len(params):
                raise ValueError("placeholder count mismatch")
            out, args = pieces[0], []
            for p, rest in zip(params, pieces[1:]):
                if p is None:
                    out += "NULL" + rest
                else:
                    out += "?" + rest
                    args.append(p)
            sql, params = out, args
        stmt = self.binding.prepare(sql)
        return stmt.bind(*params) if params else stmt

    async def execute(self, sql, params=()):
        res = await self._stmt(sql, list(params)).run()
        return int(res.meta.changes)

    async def query(self, sql, params=()):
        res = await self._stmt(sql, list(params)).all()
        # JsProxy を素の dict / list にする
        return json.loads(JSON.stringify(res.results))


class JsHttp:
    """tbf.http.Http を JS の fetch で実装"""

    async def request(self, method, url, headers=None, body=None):
        opts = {"method": method, "headers": headers or {}}
        if body is not None:
            opts["body"] = body
        res = await js_fetch(url, to_js(opts, dict_converter=Object.fromEntries))
        return int(res.status), await res.text()


class Default(WorkerEntrypoint):
    def _app(self):
        def get(key):
            value = getattr(self.env, key, None)
            return str(value) if value is not None else None

        return App(BOTS, Settings.from_env(get), D1(self.env.DB), JsHttp())

    async def scheduled(self, controller, env=None, ctx=None):
        await self._app().scheduled(str(controller.cron), int(controller.scheduledTime))

    async def fetch(self, request):
        auth = request.headers.get("authorization")
        headers = {"authorization": str(auth)} if auth else {}
        res = await self._app().handle(str(request.method), str(request.url), headers)
        return Response(res.body, status=res.status, headers={"content-type": res.content_type})
