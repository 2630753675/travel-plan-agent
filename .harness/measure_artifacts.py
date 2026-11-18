"""Measure packed vs unpacked session size and artifact footprint."""
import asyncio
import pathlib
import pickle
import sys
import tempfile

ROOT = pathlib.Path(r"E:\项目\agent_study\dsh_work2\backend")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.agents import hub  # noqa: E402
from app.artifacts import ArtifactStore  # noqa: E402
from app.tools.mock import MockSource  # noqa: E402
from verify_peer_success import ScriptedTeam, brief  # noqa: E402


def size(obj) -> int:
    return len(pickle.dumps(obj, protocol=4))


async def main():
    root = pathlib.Path(tempfile.mkdtemp())
    store = ArtifactStore(root)
    result = await hub.run_session(brief=brief(), source=MockSource(), client=ScriptedTeam(),
                                   emit=lambda *_a, **_k: None, max_turns=20, max_rounds=2,
                                   artifact_store=store)
    packed = result.session
    restored = hub._unpack_session(pickle.loads(pickle.dumps(packed)), store)
    files = list(root.rglob("*.json"))
    print(f"packed session   {size(packed)/1024:7.1f} KB   artifact_format={packed.get('artifact_format')}")
    print(f"unpacked session {size(restored)/1024:7.1f} KB")
    print(f"artifacts        {len(files)} files, {sum(f.stat().st_size for f in files)/1024:.1f} KB")
    print(f"per-snapshot saving {(size(restored)-size(packed))/1024:.1f} KB")
    print("refs", {k: (v.get('bytes') if isinstance(v, dict) else type(v).__name__) for k, v in packed.items()
                 if k in ('histories', 'bus')})


asyncio.run(main())
