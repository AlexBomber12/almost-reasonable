"""Worker-owned, durable admission ledger for finite catalog profiles."""

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
import weakref

from app.core.sqlite_files import sqlite_metadata_handle

from app.core.public_research_authority import validate_trusted_directories
from app.core.public_research_profile import ResearchProfile
from app.core.public_research_profile import PUBLIC_PURPOSES


class AuthorityJournal:
    """Reserve capacity before provider contact; a lost result consumes its slot."""

    def __init__(self, path: Path):
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("absolute authority journal required")
        validate_trusted_directories(path)
        if path.resolve(strict=True) != path:
            raise ValueError("authority journal path contains a link")
        info = path.lstat()
        parent = path.parent.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise ValueError("unsafe authority journal")
        self._path = path
        self._descriptor = sqlite_metadata_handle(path)
        self._finalizer = weakref.finalize(self, os.close, self._descriptor)
        pinned = os.fstat(self._descriptor)
        self._identity = (pinned.st_dev, pinned.st_ino)
        if self._identity != (info.st_dev, info.st_ino):
            raise ValueError("authority journal identity changed")
        with self._open() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_metadata (version TEXT NOT NULL)"
            )
            rows = db.execute("SELECT version FROM schema_metadata").fetchall()
            if not rows:
                db.execute(
                    "INSERT INTO schema_metadata VALUES ('public-research-journal-v1')"
                )
            elif rows != [("public-research-journal-v1",)]:
                raise ValueError("invalid authority journal schema")
            db.execute(
                "CREATE TABLE IF NOT EXISTS profiles (profile_id TEXT PRIMARY KEY, digest TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS reservations (operation_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(profile_id), topic_id TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS public_reservations (operation_id TEXT PRIMARY KEY, account_ref TEXT NOT NULL, topic_id TEXT NOT NULL)"
            )
            expected = {
                "schema_metadata": "CREATE TABLE schema_metadata (version TEXT NOT NULL)",
                "profiles": "CREATE TABLE profiles (profile_id TEXT PRIMARY KEY, digest TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)))",
                "reservations": "CREATE TABLE reservations (operation_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(profile_id), topic_id TEXT NOT NULL)",
                "public_reservations": "CREATE TABLE public_reservations (operation_id TEXT PRIMARY KEY, account_ref TEXT NOT NULL, topic_id TEXT NOT NULL)",
            }
            actual = dict(
                db.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
            )
            if actual != expected:
                raise ValueError("invalid authority journal schema")

    @contextmanager
    def _open(self):
        info = self._path.lstat()
        if (info.st_dev, info.st_ino) != self._identity:
            raise RuntimeError("authority journal identity changed")
        db = sqlite3.connect(self._path.as_uri() + "?mode=rw", timeout=10, uri=True)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def reserve(
        self,
        profile: ResearchProfile,
        operation_id: str,
        topic_id: str,
        *,
        question=None,
    ) -> bool:
        """One immediate transaction gates all worker processes sharing this file."""
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT digest, revoked FROM profiles WHERE profile_id=?",
                (profile.profile_id,),
            ).fetchone()
            if row != (profile.digest, 0):
                return False
            if db.execute(
                "SELECT 1 FROM reservations WHERE operation_id=?", (operation_id,)
            ).fetchone():
                return False
            aggregate = db.execute(
                "SELECT COUNT(*) FROM reservations WHERE profile_id=?",
                (profile.profile_id,),
            ).fetchone()[0]
            per_topic = db.execute(
                "SELECT COUNT(*) FROM reservations WHERE profile_id=? AND topic_id=?",
                (profile.profile_id, topic_id),
            ).fetchone()[0]
            if profile.purpose in PUBLIC_PURPOSES:
                aggregate = db.execute(
                    "SELECT COUNT(*) FROM public_reservations WHERE account_ref=?",
                    (profile.account_ref,),
                ).fetchone()[0]
                per_topic = db.execute(
                    "SELECT COUNT(*) FROM public_reservations WHERE account_ref=? AND topic_id=?",
                    (profile.account_ref, topic_id),
                ).fetchone()[0]
            topic = next(
                (item for item in profile.topics if item.topic_id == topic_id), None
            )
            if profile.question_policy is not None:
                topic = profile.topic(question)
                if topic is not None and topic.topic_id != topic_id:
                    return False
            if (
                topic is None
                or aggregate >= profile.aggregate_limit
                or per_topic >= topic.limit
            ):
                return False
            db.execute(
                "INSERT INTO reservations VALUES (?,?,?)",
                (operation_id, profile.profile_id, topic_id),
            )
            if profile.purpose in PUBLIC_PURPOSES:
                db.execute(
                    "INSERT INTO public_reservations VALUES (?,?,?)",
                    (operation_id, profile.account_ref, topic_id),
                )
        return True

    def activate(self, profile: ResearchProfile) -> None:
        """A replacement retires prior IDs without replenishing consumed slots."""
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT digest, revoked FROM profiles WHERE profile_id=?",
                (profile.profile_id,),
            ).fetchone()
            if row is not None and row != (profile.digest, 0):
                raise ValueError("replayed or changed profile authority")
            db.execute(
                "UPDATE profiles SET revoked=1 WHERE profile_id<>?",
                (profile.profile_id,),
            )
            if row is None:
                db.execute(
                    "INSERT INTO profiles(profile_id,digest) VALUES (?,?)",
                    (profile.profile_id, profile.digest),
                )

    def active(self, profile: ResearchProfile) -> bool:
        with self._open() as db:
            return db.execute(
                "SELECT digest, revoked FROM profiles WHERE profile_id=?",
                (profile.profile_id,),
            ).fetchone() == (profile.digest, 0)

    def revoke(self, profile: ResearchProfile) -> None:
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO profiles(profile_id,digest,revoked) VALUES (?,?,1)",
                (profile.profile_id, profile.digest),
            )
            db.execute(
                "UPDATE profiles SET revoked=1 WHERE profile_id=?",
                (profile.profile_id,),
            )
