"""Live proof of the Observatory API: real uvicorn, seed agents + a message,
assert the observer endpoints reflect it and send-as delivers."""
import asyncio, tempfile
import httpx, uvicorn
from postbox.api import create_app

OK = "\033[92mPASS\033[0m"
def check(label, cond): print(f"  [{OK if cond else 'FAIL'}] {label}"); assert cond, label

async def main():
    app = create_app(tempfile.mkdtemp())
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
    server = uvicorn.Server(cfg); task = asyncio.create_task(server.serve())
    while not server.started: await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
        a = (await c.post("/agents", json={"name":"alice"})).json()
        b = (await c.post("/agents", json={"name":"bob"})).json()
        await c.post("/messages", headers={"Authorization":f"Bearer {a['token']}"},
                     json={"to":"bob","body":"hi bob","subject":"greeting"})
        print("\n1. OBSERVER sees all threads")
        threads = (await c.get("/observer/threads")).json()
        check("thread visible with both members", any(set(t["members"])=={"alice","bob"} for t in threads))
        print("2. THREAD detail")
        tid = threads[0]["thread_id"]
        d = (await c.get(f"/observer/threads/{tid}")).json()
        check("detail has the message", d["messages"][0]["from"]=="alice")
        print("3. CREATE human identity + SEND AS")
        await c.post("/observer/identity", json={"name":"adam"})
        s = await c.post("/observer/send", json={"from":"adam","to":"alice","body":"ping"})
        check("send-as ok", s.status_code==201)
        check("alice now has a thread with adam", any("adam" in t["members"] for t in (await c.get("/observer/threads", params={"address":"alice"})).json()))
        print("4. UI served")
        check("/ui/ returns html", "Postbox" in (await c.get("/ui/")).text)
    server.should_exit = True; await task
    print("\n\033[92mOBSERVATORY E2E PASSED\033[0m\n")

if __name__ == "__main__":
    asyncio.run(main())
