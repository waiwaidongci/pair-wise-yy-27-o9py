from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


WITNESS_KINDS = {"version", "fragment", "transcription"}
SPECIAL_TOKENS = {"[缺页]", "[不可辨]", "[残损]", "[插入]", "[删除]"}


def validate_transcription(text: str) -> str:
    text = text.strip()
    if not text:
        raise DomainError("文本不能为空")
    unclosed = text.count("[") - text.count("]")
    if unclosed:
        raise DomainError("校勘标记括号不匹配")
    return text


class CollationDB:
    """SQLite-backed textual collation service with per-version optimistic revisions."""

    def __init__(self, path: str = "collation.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('owner','editor','reviewer'))
            );
            CREATE TABLE IF NOT EXISTS works (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              owner_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_access (
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              permission TEXT NOT NULL CHECK(permission IN ('view','review')),
              PRIMARY KEY(work_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS witnesses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              siglum TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('version','fragment','transcription')),
              source_note TEXT NOT NULL DEFAULT '',
              missing_sections TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(work_id,siglum)
            );
            CREATE TABLE IF NOT EXISTS witness_editors (
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              granted_by INTEGER NOT NULL REFERENCES users(id),
              PRIMARY KEY(witness_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS passages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              label TEXT NOT NULL,
              base_text TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','locked')),
              revision INTEGER NOT NULL DEFAULT 0,
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(work_id,label)
            );
            CREATE TABLE IF NOT EXISTS alignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              aligned_text TEXT NOT NULL,
              sort_order INTEGER NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id,witness_id)
            );
            CREATE TABLE IF NOT EXISTS variants (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id),
              base_text TEXT NOT NULL,
              proposed_text TEXT NOT NULL,
              reason TEXT NOT NULL,
              layer INTEGER NOT NULL DEFAULT 1,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            -- 每个版本（甲本/乙本）在每一段落上各自独立的修订层号，乐观锁只比对这一版。
            CREATE TABLE IF NOT EXISTS witness_revisions (
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              revision INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(passage_id,witness_id)
            );
            -- 快照按版本各自编号，互不影响。
            CREATE TABLE IF NOT EXISTS revisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              variant_id INTEGER REFERENCES variants(id) ON DELETE CASCADE,
              revision_no INTEGER NOT NULL,
              layer INTEGER NOT NULL,
              snapshot_json TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id,witness_id,revision_no)
            );
            CREATE TABLE IF NOT EXISTS notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
              body TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passage_locks (
              passage_id INTEGER PRIMARY KEY REFERENCES passages(id) ON DELETE CASCADE,
              locked_by INTEGER NOT NULL REFERENCES users(id),
              reason TEXT NOT NULL DEFAULT '',
              locked_at TEXT NOT NULL
            );
            -- 负责人选定各版最新校记后生成的合并稿；被选版本再改即失效，需重新确认。
            CREATE TABLE IF NOT EXISTS merged_drafts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              merged_text TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'confirmed' CHECK(status IN ('confirmed','stale')),
              decisions_json TEXT NOT NULL,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              confirmed_at TEXT NOT NULL,
              UNIQUE(passage_id)
            );
            """
        )
        self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        owner = self.add_user("项目负责人", "owner")
        editor = self.add_user("校勘编辑", "editor")
        work = self.create_work("一则残卷", "演示不同版本的校勘", owner)
        w1 = self.add_witness(work, "甲本", "version", "馆藏胶片", "")
        w2 = self.add_witness(work, "乙本", "fragment", "残片转录", "第二句残损")
        self.grant_witness_editor(w2, editor, owner)
        passage = self.add_passage(work, "第1节", "春水东流，故人南去。", owner)
        self.align_passage(passage, w1, "春水东流，故人南去。", 1, owner)
        self.align_passage(passage, w2, "春水东流，[不可辨][不可辨]。", 2, owner)
        variant = self.create_variant(passage, w2, "春水东流，故人南去。", "综合语义与行款补足", owner, 0)
        self.add_note(variant, "补字仍需参照纸背墨迹。", editor)

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"owner", "editor", "reviewer"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def create_work(self, title: str, description: str, owner_id: int) -> int:
        owner = self.conn.execute("SELECT role FROM users WHERE id=?", (owner_id,)).fetchone()
        if not owner or owner["role"] != "owner" or not title.strip():
            raise DomainError("作品标题或负责人无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO works(title,description,owner_id,created_at) VALUES(?,?,?,?)",
                (title.strip(), description.strip(), owner_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def grant_work_access(self, work_id: int, user_id: int, permission: str, granted_by: int) -> None:
        if permission not in {"view", "review"}:
            raise DomainError("权限必须为 view 或 review")
        self._require_owner(work_id, granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO work_access(work_id,user_id,permission) VALUES(?,?,?) "
                "ON CONFLICT(work_id,user_id) DO UPDATE SET permission=excluded.permission",
                (work_id, user_id, permission),
            )

    def _require_owner(self, work_id: int, user_id: int) -> None:
        row = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (work_id, user_id)).fetchone()
        if not row:
            raise DomainError("只有项目负责人可以执行此操作")

    def can_view_work(self, work_id: int, user_id: int) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM works WHERE id=? AND owner_id=? "
            "UNION ALL SELECT 1 FROM work_access WHERE work_id=? AND user_id=? "
            "UNION ALL SELECT 1 FROM witnesses w JOIN witness_editors e ON e.witness_id=w.id "
            "WHERE w.work_id=? AND e.user_id=? LIMIT 1",
            (work_id, user_id, work_id, user_id, work_id, user_id),
        ).fetchone())

    def can_edit_witness(self, witness_id: int, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT w.work_id,wa.permission FROM witnesses w LEFT JOIN work_access wa ON wa.work_id=w.work_id AND wa.user_id=? WHERE w.id=?",
            (user_id, witness_id),
        ).fetchone()
        if not row:
            return False
        owner = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (row["work_id"], user_id)).fetchone()
        editor = self.conn.execute("SELECT 1 FROM witness_editors WHERE witness_id=? AND user_id=?", (witness_id, user_id)).fetchone()
        return bool(owner or editor)

    def add_witness(self, work_id: int, siglum: str, kind: str, source_note: str = "", missing_sections: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone():
            raise DomainError("作品不存在")
        if not siglum.strip() or kind not in WITNESS_KINDS:
            raise DomainError("版本标识或类型无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO witnesses(work_id,siglum,kind,source_note,missing_sections,created_at) VALUES(?,?,?,?,?,?)",
                    (work_id, siglum.strip(), kind, source_note.strip(), missing_sections.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一作品中的版本标识不能重复") from exc
        return int(cur.lastrowid)

    def grant_witness_editor(self, witness_id: int, user_id: int, granted_by: int) -> None:
        witness = self.conn.execute("SELECT work_id FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not witness:
            raise DomainError("版本不存在")
        self._require_owner(witness["work_id"], granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO witness_editors(witness_id,user_id,granted_by) VALUES(?,?,?)",
                (witness_id, user_id, granted_by),
            )

    def add_passage(self, work_id: int, label: str, base_text: str, user_id: int) -> int:
        self._require_owner(work_id, user_id)
        text = validate_transcription(base_text)
        if not label.strip():
            raise DomainError("段落标签不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO passages(work_id,label,base_text,updated_by,updated_at) VALUES(?,?,?,?,?)",
                    (work_id, label.strip(), text, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("段落标签已存在") from exc
        return int(cur.lastrowid)

    def align_passage(self, passage_id: int, witness_id: int, aligned_text: str, sort_order: int, user_id: int) -> int:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if sort_order <= 0:
            raise DomainError("排序号必须大于0")
        text = validate_transcription(aligned_text)
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO alignments(passage_id,witness_id,aligned_text,sort_order,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (passage_id, witness_id, text, sort_order, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该版本已经对齐此段落") from exc
        return int(cur.lastrowid)

    # ---- 按版本各自推进的修订 ----

    def _witness_revision(self, passage_id: int, witness_id: int) -> int:
        row = self.conn.execute(
            "SELECT revision FROM witness_revisions WHERE passage_id=? AND witness_id=?",
            (passage_id, witness_id),
        ).fetchone()
        return int(row["revision"]) if row else 0

    def _set_witness_revision(self, passage_id: int, witness_id: int, revision: int) -> None:
        self.conn.execute(
            "INSERT INTO witness_revisions(passage_id,witness_id,revision) VALUES(?,?,?) "
            "ON CONFLICT(passage_id,witness_id) DO UPDATE SET revision=excluded.revision",
            (passage_id, witness_id, revision),
        )

    def create_variant(self, passage_id: int, witness_id: int, proposed_text: str, reason: str,
                       user_id: int, expected_revision: int) -> int:
        with self.transaction():
            passage, witness = self._editable_passage(passage_id, witness_id, user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            if not self.conn.execute("SELECT 1 FROM alignments WHERE passage_id=? AND witness_id=?", (passage_id, witness_id)).fetchone():
                raise DomainError("该版本尚未对齐此段落")
            if self.conn.execute("SELECT 1 FROM variants WHERE passage_id=? AND witness_id=?", (passage_id, witness_id)).fetchone():
                raise DomainError("该版本已有异文记录，请在原记录上提交新层")
            layer = self._witness_revision(passage_id, witness_id) + 1
            now = datetime.now().isoformat()
            cur = self.conn.execute(
                "INSERT INTO variants(passage_id,witness_id,base_text,proposed_text,reason,layer,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (passage_id, witness_id, passage["base_text"], text, reason.strip(), layer, user_id, now, now),
            )
            variant_id = int(cur.lastrowid)
            self._set_witness_revision(passage_id, witness_id, layer)
            self._record_revision(passage_id, witness_id, variant_id, layer, user_id)
            # 段落全局活动计数，仅用于排序与展示，不再作为乐观锁依据。
            self.conn.execute("UPDATE passages SET revision=revision+1,updated_by=?,updated_at=? WHERE id=?", (user_id, now, passage_id))
            self._mark_merge_stale(passage_id, witness_id)
        return variant_id

    def update_variant(self, variant_id: int, proposed_text: str, reason: str, user_id: int,
                       expected_revision: int) -> int:
        with self.transaction():
            variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
            if not variant:
                raise DomainError("异文记录不存在")
            passage, witness = self._editable_passage(variant["passage_id"], variant["witness_id"], user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            layer = self._witness_revision(variant["passage_id"], variant["witness_id"]) + 1
            now = datetime.now().isoformat()
            self.conn.execute(
                "UPDATE variants SET proposed_text=?,reason=?,layer=?,updated_at=? WHERE id=?",
                (text, reason.strip(), layer, now, variant_id),
            )
            self._set_witness_revision(variant["passage_id"], variant["witness_id"], layer)
            self._record_revision(variant["passage_id"], variant["witness_id"], variant_id, layer, user_id)
            self.conn.execute("UPDATE passages SET revision=revision+1,updated_by=?,updated_at=? WHERE id=?", (user_id, now, variant["passage_id"]))
            self._mark_merge_stale(variant["passage_id"], variant["witness_id"])
        return layer

    def _editable_passage(self, passage_id: int, witness_id: int, user_id: int, expected_revision: int):
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if passage["status"] == "locked" or self.conn.execute("SELECT 1 FROM passage_locks WHERE passage_id=?", (passage_id,)).fetchone():
            raise DomainError("段落已锁定，不能修改")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        current = self._witness_revision(passage_id, witness_id)
        if current != expected_revision:
            raise DomainError(f"版本冲突：当前版本修订为 {current}，提交基于 {expected_revision}")
        return passage, witness

    def _record_revision(self, passage_id: int, witness_id: int, variant_id: int, layer: int, user_id: int) -> int:
        snapshot = {
            "passage": dict(self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()),
            "variant": dict(self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()),
            "alignments": [dict(r) for r in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind FROM alignments a JOIN witnesses w ON w.id=a.witness_id WHERE a.passage_id=? ORDER BY a.sort_order",
                (passage_id,),
            ).fetchall()],
        }
        self.conn.execute(
            "INSERT INTO revisions(passage_id,witness_id,variant_id,revision_no,layer,snapshot_json,author_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (passage_id, witness_id, variant_id, layer, layer, json.dumps(snapshot, ensure_ascii=False), user_id, datetime.now().isoformat()),
        )
        return layer

    def add_note(self, variant_id: int, body: str, author_id: int) -> int:
        variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
        if not variant or not self.can_view_work(
            self.conn.execute("SELECT work_id FROM passages WHERE id=?", (variant["passage_id"],)).fetchone()["work_id"], author_id
        ):
            raise DomainError("异文不存在或无权评论")
        if not body.strip():
            raise DomainError("注释不能为空")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO notes(variant_id,body,author_id,created_at) VALUES(?,?,?,?)",
                (variant_id, body.strip(), author_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def lock_passage(self, passage_id: int, user_id: int, reason: str = "") -> None:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        self._require_owner(passage["work_id"], user_id)
        with self.transaction():
            self.conn.execute("UPDATE passages SET status='locked',updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), passage_id))
            self.conn.execute(
                "INSERT OR REPLACE INTO passage_locks(passage_id,locked_by,reason,locked_at) VALUES(?,?,?,?)",
                (passage_id, user_id, reason.strip(), datetime.now().isoformat()),
            )

    def get_snapshot(self, passage_id: int, witness_id: int, revision_no: int, user_id: int) -> dict:
        passage = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or not self.can_view_work(passage["work_id"], user_id):
            raise DomainError("无权查看该快照")
        row = self.conn.execute(
            "SELECT * FROM revisions WHERE passage_id=? AND witness_id=? AND revision_no=?",
            (passage_id, witness_id, revision_no),
        ).fetchone()
        if not row:
            raise DomainError("快照不存在")
        return {
            "passage_id": row["passage_id"],
            "witness_id": row["witness_id"],
            "revision_no": row["revision_no"],
            "layer": row["layer"],
            "created_at": row["created_at"],
            "snapshot": json.loads(row["snapshot_json"]),
        }

    # ---- 合并稿：负责人选定各版最新校记后生成；被选版本再改即失效 ----

    def _latest_variant(self, passage_id: int, witness_id: int):
        return self.conn.execute(
            "SELECT * FROM variants WHERE passage_id=? AND witness_id=? ORDER BY layer DESC, id DESC LIMIT 1",
            (passage_id, witness_id),
        ).fetchone()

    def _mark_merge_stale(self, passage_id: int, witness_id: int) -> None:
        merge = self.conn.execute(
            "SELECT * FROM merged_drafts WHERE passage_id=? AND status='confirmed'",
            (passage_id,),
        ).fetchone()
        if not merge:
            return
        decisions = json.loads(merge["decisions_json"])
        if any(int(d["witness_id"]) == witness_id for d in decisions):
            self.conn.execute("UPDATE merged_drafts SET status='stale' WHERE id=?", (merge["id"],))

    def create_merge(self, work_id: int, passage_id: int, user_id: int, merged_text: str, decisions: list) -> int:
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        if not work:
            raise DomainError("作品不存在")
        self._require_owner(work_id, user_id)
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or passage["work_id"] != work_id:
            raise DomainError("段落不存在")
        text = validate_transcription(merged_text)
        if not decisions:
            raise DomainError("请至少选择一版校记")
        recorded = []
        seen = set()
        for d in decisions:
            witness_id = int(d.get("witness_id", 0))
            if witness_id in seen:
                raise DomainError("同一版本只能选择一次")
            seen.add(witness_id)
            witness = self.conn.execute("SELECT * FROM witnesses WHERE id=? AND work_id=?", (witness_id, work_id)).fetchone()
            if not witness:
                raise DomainError("版本不存在")
            variant_id = int(d.get("variant_id", 0) or 0)
            if variant_id:
                variant = self.conn.execute(
                    "SELECT * FROM variants WHERE id=? AND passage_id=? AND witness_id=?",
                    (variant_id, passage_id, witness_id),
                ).fetchone()
            else:
                variant = self._latest_variant(passage_id, witness_id)
            if not variant:
                raise DomainError(f"版本「{witness['siglum']}」尚无校记")
            recorded.append({
                "witness_id": witness_id,
                "siglum": witness["siglum"],
                "variant_id": int(variant["id"]),
                "revision": self._witness_revision(passage_id, witness_id),
                "layer": int(variant["layer"]),
            })
        now = datetime.now().isoformat()
        with self.transaction():
            existing = self.conn.execute("SELECT id FROM merged_drafts WHERE passage_id=?", (passage_id,)).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE merged_drafts SET merged_text=?,status='confirmed',decisions_json=?,created_by=?,created_at=?,confirmed_at=? WHERE id=?",
                    (text, json.dumps(recorded, ensure_ascii=False), user_id, now, now, existing["id"]),
                )
                merge_id = int(existing["id"])
            else:
                cur = self.conn.execute(
                    "INSERT INTO merged_drafts(work_id,passage_id,merged_text,status,decisions_json,created_by,created_at,confirmed_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (work_id, passage_id, text, "confirmed", json.dumps(recorded, ensure_ascii=False), user_id, now, now),
                )
                merge_id = int(cur.lastrowid)
        return merge_id

    def _merge_for(self, passage_id: int):
        row = self.conn.execute("SELECT * FROM merged_drafts WHERE passage_id=?", (passage_id,)).fetchone()
        if not row:
            return None
        merge = dict(row)
        merge["decisions"] = json.loads(merge["decisions_json"])
        return merge

    def export_collation(self, work_id: int, user_id: int) -> dict:
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该校勘项目")
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        witnesses = [dict(r) for r in self.conn.execute("SELECT * FROM witnesses WHERE work_id=? ORDER BY id", (work_id,))]
        siglum_of = {w["id"]: w["siglum"] for w in witnesses}
        merges = {m["passage_id"]: self._merge_for(m["passage_id"]) for m in self.conn.execute(
            "SELECT * FROM merged_drafts WHERE work_id=?", (work_id,)
        ).fetchall()}
        passages_out = []
        merged_drafts = []
        conflicts = []
        undecided_passages = []
        gaps = 0
        for passage in self.conn.execute("SELECT * FROM passages WHERE work_id=? ORDER BY id", (work_id,)).fetchall():
            alignments = []
            for row in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind,w.missing_sections FROM alignments a JOIN witnesses w ON w.id=a.witness_id "
                "WHERE a.passage_id=? ORDER BY a.sort_order", (passage["id"],)
            ).fetchall():
                item = dict(row)
                if "[缺页]" in item["aligned_text"] or "[残损]" in item["aligned_text"]:
                    item["has_gap"] = True
                    gaps += 1
                alignments.append(item)
            variants = []
            for row in self.conn.execute("SELECT * FROM variants WHERE passage_id=? ORDER BY witness_id,layer,id", (passage["id"],)).fetchall():
                variant = dict(row)
                variant["notes"] = [dict(r) for r in self.conn.execute("SELECT * FROM notes WHERE variant_id=? ORDER BY id", (row["id"],))]
                variants.append(variant)
            variant_witnesses = {v["witness_id"] for v in variants}
            fresh = merges.get(passage["id"]) if merges.get(passage["id"], {}).get("status") == "confirmed" else None
            stale = merges.get(passage["id"]) if merges.get(passage["id"], {}).get("status") == "stale" else None
            p_conflicts = []
            merge_info = None
            stale_info = None
            undecided = False
            if fresh:
                covered = {d["witness_id"] for d in fresh["decisions"]}
                merge_info = {
                    "merged_text": fresh["merged_text"],
                    "decisions": fresh["decisions"],
                    "confirmed_at": fresh["confirmed_at"],
                }
                merged_drafts.append({
                    "passage_id": passage["id"],
                    "label": passage["label"],
                    "merged_text": fresh["merged_text"],
                    "decisions": fresh["decisions"],
                    "confirmed_at": fresh["confirmed_at"],
                })
                uncovered = variant_witnesses - covered
                if uncovered:
                    p_conflicts = [{"witness_id": wid, "siglum": siglum_of[wid]} for wid in sorted(uncovered)]
            elif stale:
                changed = [
                    {"witness_id": d["witness_id"], "siglum": d["siglum"]}
                    for d in stale["decisions"]
                    if self._witness_revision(passage["id"], d["witness_id"]) > int(d["revision"])
                ]
                stale_info = {
                    "merged_text": stale["merged_text"],
                    "decisions": stale["decisions"],
                    "confirmed_at": stale["confirmed_at"],
                    "changed_witnesses": changed,
                }
                # 旧合并稿已失效、等待重新确认；该段各版校记都不能再当作合并稿导出。
                undecided = True
                p_conflicts = [{"witness_id": wid, "siglum": siglum_of[wid]} for wid in sorted(variant_witnesses)]
            else:
                if variant_witnesses:
                    undecided = True
                    p_conflicts = [{"witness_id": wid, "siglum": siglum_of[wid]} for wid in sorted(variant_witnesses)]
            if p_conflicts:
                conflicts.append({"passage_id": passage["id"], "label": passage["label"], "witnesses": p_conflicts})
            if undecided:
                undecided_passages.append({"passage_id": passage["id"], "label": passage["label"], "witnesses": p_conflicts})
            passages_out.append({
                **dict(passage),
                "alignments": alignments,
                "variants": variants,
                "merge": merge_info,
                "stale_merge": stale_info,
                "undecided": undecided,
                "conflicts": p_conflicts,
            })
        return {
            "work": dict(work),
            "witnesses": witnesses,
            "passages": passages_out,
            "gap_count": gaps,
            "merged_drafts": merged_drafts,
            "conflicts": conflicts,
            "undecided_passages": undecided_passages,
        }

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "works": [dict(r) for r in self.conn.execute("SELECT * FROM works ORDER BY id")],
            "witnesses": [dict(r) for r in self.conn.execute("SELECT * FROM witnesses ORDER BY id")],
            "passages": [dict(r) for r in self.conn.execute("SELECT * FROM passages ORDER BY id")],
        }
