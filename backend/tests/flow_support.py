"""Stage 15 Milestone B fixtures: a synthetic FastAPI + React repository and the full stack."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from buildlens.application.architecture import ArchitectureService
from buildlens.application.flow import FlowService
from buildlens.application.flow_evidence import FlowEvidenceService
from buildlens.infrastructure.persistence.architecture import SqlAlchemyArchitectureStore
from buildlens.infrastructure.persistence.flow import SqlAlchemyFlowStore
from buildlens.infrastructure.persistence.flow_evidence import SqlAlchemyFlowEvidenceStore
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader
from tests.semantic_support import CatalogStack, write

FLOW_REPOSITORY = {
    "backend/pyproject.toml": '[project]\nname = "shop"\ndependencies = ["fastapi>=0.115,<1"]\n',
    "backend/uv.lock": 'version = 1\n\n[[package]]\nname = "fastapi"\nversion = "0.141.1"\n',
    "backend/app/__init__.py": "",
    "backend/app/models.py": (
        "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column\n\n\n"
        "class Base(DeclarativeBase):\n    pass\n\n\n"
        "class Item(Base):\n"
        "    __tablename__ = 'items'\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n"
        "    name: Mapped[str]\n"
    ),
    "backend/app/db.py": (
        "from sqlalchemy.orm import Session, sessionmaker\n\n"
        "factory = sessionmaker()\n\n\n"
        "def get_db():\n"
        "    session = factory()\n"
        "    try:\n"
        "        yield session\n"
        "    finally:\n"
        "        session.close()\n"
    ),
    "backend/app/service.py": (
        "from sqlalchemy import select\n"
        "from sqlalchemy.orm import Session\n\n"
        "from app.models import Item\n\n\n"
        "class ItemService:\n"
        "    def __init__(self, session: Session) -> None:\n"
        "        self.session = session\n\n"
        "    def create(self, name: str) -> Item:\n"
        "        item = Item(name=name)\n"
        "        self.session.add(item)\n"
        "        self.session.commit()\n"
        "        return item\n\n"
        "    def listing(self) -> list[Item]:\n"
        "        return list(self.session.scalars(select(Item)))\n"
    ),
    "backend/app/api.py": (
        "from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException\n"
        "from sqlalchemy.orm import Session\n\n"
        "from app.db import get_db\n"
        "from app.service import ItemService\n\n"
        "router = APIRouter(prefix='/api/items')\n\n\n"
        "def get_service(session: Session = Depends(get_db)) -> ItemService:\n"
        "    return ItemService(session)\n\n\n"
        "def notify(name: str) -> None:\n"
        "    print(name)\n\n\n"
        "@router.post('', status_code=201)\n"
        "def create_item(\n"
        "    name: str, tasks: BackgroundTasks, svc: ItemService = Depends(get_service)\n"
        "):\n"
        "    if not name:\n"
        "        raise HTTPException(status_code=400)\n"
        "    item = svc.create(name)\n"
        "    tasks.add_task(notify, name)\n"
        "    return {'id': item.id}\n\n\n"
        "@router.get('')\n"
        "def list_items(svc: ItemService = Depends(get_service)):\n"
        "    return svc.listing()\n"
    ),
    "backend/app/main.py": (
        "from fastapi import FastAPI\n\n"
        "from app.api import router\n\n"
        "app = FastAPI()\n"
        "app.include_router(router)\n"
    ),
    "frontend/package.json": json.dumps({"name": "web", "dependencies": {"react": "^18.3.1"}}),
    "frontend/src/client.ts": (
        "const API_BASE = 'http://127.0.0.1:8000';\n\n"
        "export async function request(path: string, init?: RequestInit) {\n"
        "  const response = await fetch(`${API_BASE}${path}`, init);\n"
        "  if (!response.ok) {\n"
        "    throw new Error('failed');\n"
        "  }\n"
        "  return response.json();\n"
        "}\n"
    ),
    "frontend/src/items.ts": (
        "import { request } from './client';\n\n"
        "export function createItem(name: string) {\n"
        "  return request('/api/items', { method: 'POST', body: JSON.stringify({ name }) });\n"
        "}\n\n"
        "export function listItems() {\n"
        "  return request('/api/items');\n"
        "}\n"
    ),
    "frontend/src/AddItem.tsx": (
        "import { useEffect, useState } from 'react';\n"
        "import { createItem, listItems } from './items';\n\n"
        "export function AddItem() {\n"
        "  const [items, setItems] = useState([]);\n"
        "  const save = async () => {\n"
        "    await createItem('x');\n"
        "    setItems(await listItems());\n"
        "  };\n"
        "  useEffect(() => { listItems(); }, [items]);\n"
        "  return <button onClick={save}>Add</button>;\n"
        "}\n"
    ),
}


# Designed acceptance cases (Milestone B). Every case has a designed verdict in
# tests/integration/test_flow_model.py; names in the decoy files are deliberate non-links.
DESIGNED_REPOSITORY = {
    **FLOW_REPOSITORY,
    "backend/app/main.py": (
        "from fastapi import FastAPI\n\n"
        "from app.api import router\n"
        "from app.responses import router as extra\n\n"
        "app = FastAPI()\n"
        "app.include_router(router)\n"
        "app.include_router(extra)\n"
    ),
    "backend/app/responses.py": (
        "from fastapi import APIRouter, Response\n"
        "from fastapi.responses import JSONResponse\n\n"
        "router = APIRouter(prefix='/api/extra')\n\n\n"
        "@router.post('/plain')\n"
        "def plain():\n"
        "    return {'ok': True}\n\n\n"
        "@router.post('/json')\n"
        "def as_json():\n"
        "    return JSONResponse({'ok': True}, status_code=202)\n\n\n"
        "@router.post('/dynamic')\n"
        "def dynamic(response: Response, flag: bool):\n"
        "    if flag:\n"
        "        response.status_code = 203\n"
        "    return {'ok': True}\n"
    ),
    "backend/app/cases.py": (
        "import asyncio\n"
        "from dataclasses import dataclass\n"
        "from enum import Enum\n\n"
        "from sqlalchemy import text\n"
        "from sqlalchemy.orm import Session\n\n"
        "from app.models import Item\n\n\n"
        "class Color(Enum):\n    RED = 1\n\n\n"
        "class Meta(type):\n"
        "    def __call__(cls, *args, **kwargs):\n"
        "        return super().__call__(*args, **kwargs)\n\n\n"
        "class Managed(metaclass=Meta):\n"
        "    def __init__(self) -> None:\n        self.ready = True\n\n\n"
        "class Other:\n    pass\n\n\n"
        "class Swapped:\n"
        "    def __new__(cls):\n        return Other()\n\n"
        "    def __init__(self) -> None:\n        self.never = True\n\n\n"
        "class Allocated:\n"
        "    def __new__(cls):\n        return super().__new__(cls)\n\n"
        "    def __init__(self) -> None:\n        self.ok = True\n\n\n"
        "@dataclass\nclass Point:\n    x: int = 0\n\n"
        "    def __post_init__(self) -> None:\n        self.x += 1\n\n\n"
        "@dataclass(init=False)\nclass Manual:\n    x: int = 0\n\n"
        "    def __post_init__(self) -> None:\n        self.x += 1\n\n\n"
        "@dataclass\nclass Explicit:\n    x: int = 0\n\n"
        "    def __init__(self, x: int) -> None:\n        self.x = x\n\n"
        "    def __post_init__(self) -> None:\n        self.x += 1\n\n\n"
        "class Shape:\n"
        "    def area(self) -> int:\n        return self.size()\n\n"
        "    def size(self) -> int:\n        return 1\n\n\n"
        "class Square(Shape):\n"
        "    def size(self) -> int:\n        return 4\n\n\n"
        "class Alpha:\n    def go(self) -> int:\n        return 1\n\n\n"
        "class Beta:\n    def go(self) -> int:\n        return 2\n\n\n"
        "def construct_all() -> None:\n"
        "    Color(1)\n    Managed()\n    Swapped()\n    Allocated()\n"
        "    Point()\n    Manual()\n    Explicit(1)\n    Shape().area()\n\n\n"
        "def receivers() -> None:\n"
        "    svc = Alpha()\n    svc = Beta()\n    svc.go()\n\n\n"
        "async def fetch() -> int:\n    return 1\n\n\n"
        "def countdown():\n    yield 1\n\n\n"
        "def never_awaited() -> None:\n    fetch()\n\n\n"
        "def never_iterated() -> None:\n    countdown()\n\n\n"
        "async def awaited() -> int:\n"
        "    task = asyncio.create_task(fetch())\n"
        "    await task\n"
        "    return await fetch()\n\n\n"
        "def raw(session: Session) -> None:\n"
        "    session.execute(text('select * from items'))\n\n\n"
        "def staged(session: Session) -> None:\n"
        "    session.add(Item(name='x'))\n\n\n"
        "def lookalike() -> None:\n"
        "    helper()\n"
        "    getattr(Alpha(), 'go')()\n"
    ),
    "backend/app/other.py": "def helper() -> int:\n    return 1\n",
    "backend/app/decoys.py": (
        "def һelper() -> int:\n    return 2\n\n\ndef caller() -> int:\n    return helper()\n"
    ),
    "frontend/package.json": json.dumps(
        {
            "name": "web",
            "dependencies": {
                "react": "^18.3.1",
                "@tanstack/react-query": "5.104.1",
                "react-hook-form": "7.88.0",
                "@hey-api/client-fetch": "0.10.0",
            },
        }
    ),
    "frontend/tsconfig.json": json.dumps(
        {"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}
    ),
    "frontend/src/sdk.ts": (
        "import { createClient } from '@hey-api/client-fetch';\n\n"
        "export const client = createClient({ baseUrl: 'http://127.0.0.1:8000' });\n\n"
        "export function createItemSdk(name: string) {\n"
        "  return client.post({ url: '/api/items', body: { name } });\n"
        "}\n\n"
        "const fake = { GET(path: string) { return path; } };\n\n"
        "export function decoy() {\n"
        "  return fake.GET('/api/items');\n"
        "}\n"
    ),
    "frontend/src/Panel.tsx": (
        "import { useCallback, useMemo, useState } from 'react';\n"
        "import { useMutation } from '@tanstack/react-query';\n"
        "import { useForm } from 'react-hook-form';\n"
        "import { useMutation as useOther } from 'other-query';\n"
        "import { createItem } from '@/items';\n"
        "import { createItemSdk } from './sdk';\n\n"
        "function Child({ onDone }: { onDone: () => void }) {\n"
        "  return <button onClick={() => onDone()}>done</button>;\n"
        "}\n\n"
        "export function Panel({ open }: { open: boolean }) {\n"
        "  const [count, setCount] = useState(0);\n"
        "  const { handleSubmit } = useForm();\n"
        "  const mutation = useMutation({\n"
        "    mutationFn: (name: string) => createItem(name),\n"
        "    onSuccess: () => setCount(1),\n"
        "  });\n"
        "  const other = useOther({ mutationFn: () => createItemSdk('y') });\n"
        "  const memo = useMemo(() => count * 2, [count]);\n"
        "  const onSave = useCallback(() => { mutation.mutate('x'); }, [mutation]);\n"
        "  const onValid = () => { createItemSdk('z'); };\n"
        "  const later = () => { other.mutate(); };\n"
        "  return (\n"
        "    <form onSubmit={handleSubmit(onValid)}>\n"
        "      {open && <button onClick={onSave}>save</button>}\n"
        "      <button onClick={later}>later</button>\n"
        "      <Child onDone={onSave} />\n"
        "      <span>{memo}</span>\n"
        "    </form>\n"
        "  );\n"
        "}\n"
    ),
    "frontend/src/Decoy.tsx": (
        "function handleSubmit(f: () => void) {\n  return f;\n}\n\n"
        "export function Decoy() {\n"
        "  const go = () => { console.log('go'); };\n"
        "  return <form onSubmit={handleSubmit(go)}></form>;\n"
        "}\n"
    ),
}


# The remaining Milestone B families: generators and context managers, decorators, NoReturn,
# Protocol candidates, star/conditional imports, transaction scopes, the FastAPI response
# boundary and exception paths, React effect cleanup and re-runs, query invalidation, timers and
# promise reactions, and TS construction.
SEMANTICS_REPOSITORY = {
    **FLOW_REPOSITORY,
    "backend/app/main.py": (
        "from fastapi import FastAPI\n\n"
        "from app.advanced import install, router as advanced\n"
        "from app.api import router\n\n"
        "app = FastAPI()\n"
        "app.include_router(router)\n"
        "app.include_router(advanced)\n"
        "install(app)\n"
    ),
    "backend/app/generators.py": (
        "import contextlib\n"
        "import functools\n"
        "import sys\n"
        "from typing import NoReturn, Protocol\n\n"
        "from sqlalchemy.orm import Session, sessionmaker\n\n"
        "from app.models import Item\n\n"
        "factory = sessionmaker()\n\n\n"
        "def numbers():\n    yield 1\n    yield 2\n\n\n"
        "def consume() -> int:\n"
        "    total = 0\n"
        "    for n in numbers():\n        total += n\n"
        "    g = numbers()\n"
        "    next(g)\n"
        "    g.send(None)\n"
        "    g.close()\n"
        "    return total\n\n\n"
        "@contextlib.contextmanager\n"
        "def opened():\n"
        "    try:\n        yield 1\n    finally:\n        print('close')\n\n\n"
        "def use_manager() -> None:\n"
        "    with opened() as value:\n        print(value)\n\n\n"
        "def cached(fn):\n"
        "    @functools.wraps(fn)\n"
        "    def inner(*args):\n        return fn(*args)\n\n"
        "    return inner\n\n\n"
        "@cached\ndef wrapped() -> int:\n    return 1\n\n\n"
        "def plain() -> int:\n    return 2\n\n\n"
        "def fail() -> NoReturn:\n    raise RuntimeError('x')\n\n\n"
        "def spin():\n    while True:\n        pass\n\n\n"
        "def callers() -> None:\n"
        "    wrapped()\n    plain()\n    fail()\n    spin()\n    sys.exit(1)\n\n\n"
        "class Store(Protocol):\n"
        "    def save(self, item: Item) -> None: ...\n\n"
        "    def load(self) -> Item: ...\n\n\n"
        "class SqlStore:\n"
        "    def save(self, item: Item) -> None:\n        pass\n\n"
        "    def load(self) -> Item:\n        return Item()\n\n\n"
        "class HalfStore:\n"
        "    def save(self, item: Item) -> None:\n        pass\n\n\n"
        "def persist(store: Store, item: Item) -> None:\n    store.save(item)\n\n\n"
        "def transactional(session: Session) -> None:\n"
        "    with session.begin():\n        session.add(Item(name='a'))\n"
        "    session.add(Item(name='b'))\n\n\n"
        "def factory_transaction() -> None:\n"
        "    with factory.begin() as session:\n        session.add(Item(name='c'))\n\n\n"
        "class Ledger:\n"
        "    def begin(self):\n        return self\n\n"
        "    def __enter__(self):\n        return self\n\n"
        "    def __exit__(self, *args):\n        return False\n\n\n"
        "def decoys(session: Session) -> None:\n"
        "    with Ledger().begin():\n        session.add(Item(name='d'))\n"
        "    for value in [1, 2]:\n        print(value)\n"
    ),
    "backend/app/star.py": (
        "from app.other import *\n\n"
        "try:\n    import ujson as json\nexcept ImportError:\n    import json\n\n\n"
        "def starred() -> int:\n    return helper()\n\n\n"
        "def dumps(x):\n    return json.dumps(x)\n"
    ),
    "backend/app/other.py": "def helper() -> int:\n    return 1\n",
    "backend/app/advanced.py": (
        "from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Request\n"
        "from fastapi.exceptions import RequestValidationError\n"
        "from fastapi.responses import JSONResponse\n"
        "from sqlalchemy.orm import Session\n"
        "from starlette.background import BackgroundTask\n\n"
        "from app.db import get_db\n\n"
        "router = APIRouter(prefix='/api/advanced')\n\n\n"
        "def current_user(token: str):\n"
        "    if not token:\n        raise HTTPException(status_code=401)\n"
        "    return token\n\n\n"
        "def audit(session: Session = Depends(get_db)) -> Session:\n    return session\n\n\n"
        "def check(name: str) -> None:\n"
        "    if name == 'x':\n        raise HTTPException(status_code=409)\n\n\n"
        "def cleanup(session: Session) -> None:\n    session.close()\n\n\n"
        "@router.post('/guarded')\n"
        "def guarded(\n"
        "    name: str,\n"
        "    tasks: BackgroundTasks,\n"
        "    user: str = Depends(current_user),\n"
        "    session: Session = Depends(audit, use_cache=False),\n"
        "):\n"
        "    check(name)\n"
        "    tasks.add_task(cleanup, session)\n"
        "    return JSONResponse({'ok': True}, background=BackgroundTask(cleanup, session))\n\n\n"
        "async def on_http(request: Request, exc: HTTPException):\n"
        "    return JSONResponse({'detail': 'x'}, status_code=exc.status_code)\n\n\n"
        "async def on_validation(request: Request, exc: RequestValidationError):\n"
        "    return JSONResponse({'detail': 'bad'}, status_code=400)\n\n\n"
        "def install(app: FastAPI) -> None:\n"
        "    app.add_exception_handler(HTTPException, on_http)\n"
        "    app.add_exception_handler(RequestValidationError, on_validation)\n"
    ),
    "frontend/package.json": json.dumps(
        {"name": "web", "dependencies": {"react": "^18.3.1", "@tanstack/react-query": "5.104.1"}}
    ),
    "frontend/src/Effects.tsx": (
        "import { useEffect, useState } from 'react';\n"
        "import { useQueryClient } from '@tanstack/react-query';\n\n"
        "export function Ticker() {\n"
        "  const [tick, setTick] = useState(0);\n"
        "  const [other, setOther] = useState(0);\n"
        "  const client = useQueryClient();\n"
        "  useEffect(() => {\n"
        "    const id = setInterval(() => setTick(1), 1000);\n"
        "    return () => clearInterval(id);\n"
        "  }, [tick]);\n"
        "  useEffect(() => { document.title = 'x'; }, []);\n"
        "  const refresh = () => {\n"
        "    setOther(2);\n"
        "    client.invalidateQueries({ queryKey: ['items'] });\n"
        "    fetch('/x').then((r) => r.json());\n"
        "  };\n"
        "  return <button onClick={refresh}>{tick}{other}</button>;\n"
        "}\n"
    ),
    "frontend/src/classes.ts": (
        "class Base {\n"
        "  constructor() {\n    this.init();\n  }\n\n"
        "  init() {\n    return 1;\n  }\n"
        "}\n\n"
        "export class Child extends Base {\n  count = 0;\n}\n\n"
        "export class Explicit extends Base {\n"
        "  constructor() {\n    super();\n  }\n"
        "}\n\n"
        "class Weird {\n"
        "  constructor() {\n    return { fake: true };\n  }\n"
        "}\n\n"
        "export function build() {\n"
        "  new Child();\n  new Explicit();\n  new Weird();\n"
        "}\n"
    ),
}


# Milestone C oracles: feasibility in the sound fragment, a commit in dependency teardown, and
# scopes with designed guards (the trace engine's verdicts are checked against these).
TRACE_REPOSITORY = {
    **FLOW_REPOSITORY,
    "backend/app/main.py": (
        "from fastapi import FastAPI\n\n"
        "from app.api import router\n"
        "from app.tx import router as tx\n\n"
        "app = FastAPI()\n"
        "app.include_router(router)\n"
        "app.include_router(tx)\n"
    ),
    "backend/app/tx.py": (
        "from fastapi import APIRouter, Depends\n"
        "from sqlalchemy.orm import Session, sessionmaker\n\n"
        "from app.models import Item\n\n"
        "factory = sessionmaker()\n"
        "router = APIRouter(prefix='/api/tx')\n\n\n"
        "def get_tx():\n"
        "    session = factory()\n"
        "    try:\n        yield session\n        session.commit()\n"
        "    finally:\n        session.close()\n\n\n"
        "@router.post('')\n"
        "def tx_item(name: str, session: Session = Depends(get_tx)):\n"
        "    session.add(Item(name=name))\n"
        "    return {'ok': True}\n"
    ),
    "backend/app/feasible.py": (
        "class Thing:\n"
        "    def __eq__(self, other):\n        return True\n\n\n"
        "def target() -> None:\n    pass\n\n\n"
        "def nones(v):\n"
        "    if v is None:\n"
        "        if v is not None:\n            target()\n"
        "        target()\n\n\n"
        "def literal() -> None:\n"
        "    x = 1\n"
        "    if x == 2:\n        target()\n"
        "    if x == 1:\n        target()\n\n\n"
        "def overridden() -> None:\n"
        "    x = Thing()\n"
        "    if x == 1:\n        target()\n\n\n"
        "def lonely() -> int:\n    return 1\n"
    ),
    "frontend/src/feasible.ts": (
        "function target() {\n  return 1;\n}\n\n"
        "export function strict(x: number) {\n"
        "  if (x === 1) {\n    if (x === 2) {\n      target();\n    }\n  }\n}\n\n"
        "export function loose(x: any) {\n  if (x == 1) {\n    target();\n  }\n}\n\n"
        "export function getter(o: { ok: boolean }) {\n  if (o.ok) {\n    target();\n  }\n}\n\n"
        "export function twice(check: () => boolean) {\n"
        "  if (check()) {\n    if (!check()) {\n      target();\n    }\n  }\n}\n\n"
        "export function rebound() {\n"
        "  let x = 1;\n"
        "  const bump = () => {\n    x = 2;\n  };\n"
        "  bump();\n"
        "  if (x === 1) {\n    if (x === 2) {\n      target();\n    }\n  }\n}\n\n"
        "function orphan() {\n  return 2;\n}\n\n"
        "export function types(v: unknown) {\n"
        "  if (typeof v === 'string') {\n"
        "    if (typeof v === 'number') {\n      target();\n    }\n  }\n}\n"
    ),
}


class FlowStack:
    """The real stack from snapshot to flow model over a synthetic repository."""

    def __init__(self, engine: Engine, root: Path, files: dict[str, str] | None = None) -> None:
        write(root, files or FLOW_REPOSITORY)
        self.stack = CatalogStack(engine, root, "Flow B")
        self.snapshot = self.stack.prepared()
        self.project_id, self.snapshot_id = self.stack.project.id, self.snapshot.id
        self.stack.catalog.build(self.project_id, self.snapshot_id)
        self.architecture = ArchitectureService(
            SqlAlchemyArchitectureStore(self.stack.factory), self.stack.catalog
        )
        self.architecture.build(self.project_id, self.snapshot_id)
        self.evidence = FlowEvidenceService(
            SqlAlchemyFlowEvidenceStore(self.stack.factory), SupervisedSourceReader
        )
        self.evidence.extract(self.project_id, self.snapshot_id)
        self.store = SqlAlchemyFlowStore(self.stack.factory)
        self.flow = FlowService(self.store, self.evidence, self.stack.catalog, self.architecture)

    def build(self) -> Any:
        return self.flow.build(self.project_id, self.snapshot_id)

    def edges(self, run: Any, **filters: Any) -> list[Any]:
        return self.store.edges(run.id, limit=100_000, **filters)[0]
