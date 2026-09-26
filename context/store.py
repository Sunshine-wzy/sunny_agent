"""SQLite repository. Called in a worker thread; never holds a transaction over I/O."""

import json
import secrets
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .models import ContextError, ConversationRef, Scope


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ContextStore:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise ContextError(f"Unsupported context database version: {version}")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, short_id TEXT NOT NULL,
                title TEXT NOT NULL, source_key TEXT, creation_key TEXT NOT NULL,
                request TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scope, short_id), UNIQUE(scope, source_key),
                UNIQUE(scope, creation_key), UNIQUE(scope, id)
            );
            CREATE TABLE IF NOT EXISTS active_conversations (
                scope TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                FOREIGN KEY(scope, conversation_id) REFERENCES conversations(scope, id)
            );
            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, message_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '', UNIQUE(scope, message_id),
                FOREIGN KEY(scope, conversation_id) REFERENCES conversations(scope, id)
            );
            CREATE TABLE IF NOT EXISTS context_entries (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                kind TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, turn_id TEXT REFERENCES turns(id),
                UNIQUE(conversation_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS entries_by_conversation
                ON context_entries(conversation_id, seq);
            CREATE TABLE IF NOT EXISTS message_bindings (
                scope TEXT NOT NULL, message_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, content TEXT NOT NULL,
                sender_name TEXT NOT NULL, sender_id TEXT NOT NULL,
                entry_ids TEXT NOT NULL, PRIMARY KEY(scope, message_id),
                FOREIGN KEY(scope, conversation_id) REFERENCES conversations(scope, id)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                idempotency_key TEXT NOT NULL, payload TEXT NOT NULL,
                visible TEXT NOT NULL, entry_ids TEXT NOT NULL,
                turn_id TEXT REFERENCES turns(id), status TEXT NOT NULL,
                receipt TEXT, error TEXT NOT NULL DEFAULT '',
                UNIQUE(conversation_id, idempotency_key)
            );
        """)
        with self.db:
            if version < 2:
                self.db.execute("BEGIN")
                self.db.execute(
                    "ALTER TABLE conversations ADD COLUMN title_status TEXT NOT NULL "
                    "DEFAULT 'pending'"
                )
                self.db.execute(
                    "UPDATE conversations SET title_status='fixed' "
                    "WHERE title != '新会话' OR source_key IS NOT NULL"
                )
                self.db.execute("PRAGMA user_version=2")
            self.db.execute(
                "UPDATE turns SET status='interrupted' WHERE status='running'"
            )
            self.db.execute(
                "UPDATE deliveries SET status='unknown' WHERE status='sending'"
            )

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def _conversation(self, row: sqlite3.Row) -> ConversationRef:
        adapter, bot_id, chat_type, peer_id = json.loads(row["scope"])
        return ConversationRef(
            Scope(bot_id, chat_type, peer_id, adapter),
            row["id"],
            row["short_id"],
            row["title"],
        )

    def _validate(self, conversation: ConversationRef) -> None:
        if not self.db.execute(
            "SELECT 1 FROM conversations WHERE id=? AND scope=?",
            (conversation.conversation_id, conversation.scope.key),
        ).fetchone():
            raise ContextError("会话不存在或不属于当前聊天。")

    def create(
        self,
        scope: Scope,
        title: str,
        source_key: str | None,
        key: str,
        activate: bool,
        auto_title: bool = True,
    ) -> ConversationRef:
        request = encode([title, source_key, activate])
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM conversations WHERE scope=? AND creation_key=?",
                (scope.key, key),
            ).fetchone()
            if row:
                if row["request"] != request:
                    raise ContextError("新建会话的幂等键已用于不同请求。")
                return self._conversation(row)
            if source_key:
                row = self.db.execute(
                    "SELECT * FROM conversations WHERE scope=? AND source_key=?",
                    (scope.key, source_key),
                ).fetchone()
                if row:
                    return self._conversation(row)
            while True:
                short_id = secrets.token_hex(3).upper()
                if not self.db.execute(
                    "SELECT 1 FROM conversations WHERE scope=? AND short_id=?",
                    (scope.key, short_id),
                ).fetchone():
                    break
            conversation = ConversationRef(scope, uuid.uuid4().hex, short_id, title)
            self.db.execute(
                "INSERT INTO conversations(id,scope,short_id,title,source_key,"
                "creation_key,request,title_status) VALUES(?,?,?,?,?,?,?,?)",
                (
                    conversation.conversation_id,
                    scope.key,
                    short_id,
                    title,
                    source_key,
                    key,
                    request,
                    "pending" if auto_title else "fixed",
                ),
            )
            if activate:
                self._activate(conversation)
            return conversation

    def active(self, scope: Scope) -> ConversationRef | None:
        with self.lock:
            row = self.db.execute(
                "SELECT c.* FROM conversations c JOIN active_conversations a "
                "ON c.id=a.conversation_id WHERE a.scope=?",
                (scope.key,),
            ).fetchone()
            return self._conversation(row) if row else None

    def _activate(self, conversation: ConversationRef) -> None:
        self._validate(conversation)
        self.db.execute(
            "INSERT INTO active_conversations VALUES(?,?) ON CONFLICT(scope) "
            "DO UPDATE SET conversation_id=excluded.conversation_id",
            (conversation.scope.key, conversation.conversation_id),
        )

    def switch(self, conversation: ConversationRef) -> None:
        with self.lock, self.db:
            self._activate(conversation)

    def find(self, scope: Scope, short_id: str) -> ConversationRef | None:
        with self.lock:
            identifier = short_id.strip().lstrip("#")
            row = self.db.execute(
                "SELECT * FROM conversations WHERE scope=? AND (short_id=? OR id=?)",
                (scope.key, identifier.upper(), identifier.lower()),
            ).fetchone()
            return self._conversation(row) if row else None

    def list_sessions(self, scope: Scope, limit: int) -> list[ConversationRef]:
        with self.lock:
            return [
                self._conversation(row)
                for row in self.db.execute(
                    "SELECT * FROM conversations WHERE scope=? ORDER BY rowid DESC LIMIT ?",
                    (scope.key, limit),
                )
            ]

    def session_catalog(self, scope: Scope, offset: int, limit: int) -> dict[str, Any]:
        with self.lock:
            total = self.db.execute(
                "SELECT COUNT(*) FROM conversations WHERE scope=?", (scope.key,)
            ).fetchone()[0]
            active = self.active(scope)
            rows = self.db.execute(
                "SELECT id,short_id,title,created_at FROM conversations "
                "WHERE scope=? ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (scope.key, limit, offset),
            ).fetchall()
            next_offset = offset + len(rows)
            return {
                "sessions": [
                    {
                        "session_id": row["short_id"],
                        "title": row["title"][:200],
                        "created_at": row["created_at"],
                        "is_current": bool(
                            active and row["id"] == active.conversation_id
                        ),
                    }
                    for row in rows
                ],
                "offset": offset,
                "total": total,
                "has_more": next_offset < total,
                "next_offset": next_offset if next_offset < total else None,
            }

    def title_entries(self, conversation: ConversationRef) -> list[dict[str, Any]]:
        """A small snapshot for naming; exclude failed turns and control messages."""
        with self.lock:
            self._validate(conversation)
            status = self.db.execute(
                "SELECT title_status FROM conversations WHERE id=?",
                (conversation.conversation_id,),
            ).fetchone()[0]
            if status != "pending":
                return []
            turns = self.db.execute(
                "SELECT e.kind,e.content FROM context_entries e JOIN turns t "
                "ON e.turn_id=t.id WHERE e.conversation_id=? AND e.kind='turn' "
                "AND t.status='complete' ORDER BY e.seq LIMIT 3",
                (conversation.conversation_id,),
            ).fetchall()
            if not turns:
                return []
            sources = self.db.execute(
                "SELECT kind,content FROM context_entries WHERE conversation_id=? "
                "AND kind='external' ORDER BY seq LIMIT 2",
                (conversation.conversation_id,),
            ).fetchall()
            return [
                {"kind": row["kind"], "content": json.loads(row["content"])}
                for row in [*turns, *sources]
            ]

    def set_generated_title(self, conversation: ConversationRef, title: str) -> None:
        with self.lock, self.db:
            self._validate(conversation)
            self.db.execute(
                "UPDATE conversations SET title=?,title_status='generated' "
                "WHERE id=? AND scope=? AND title_status='pending'",
                (title, conversation.conversation_id, conversation.scope.key),
            )

    def _append(
        self,
        conversation: ConversationRef,
        kind: str,
        content: Any,
        source: Any,
        key: str,
        turn_id: str | None = None,
    ) -> str:
        self._validate(conversation)
        encoded_content, encoded_source = encode(content), encode(source)
        row = self.db.execute(
            "SELECT * FROM context_entries WHERE conversation_id=? AND idempotency_key=?",
            (conversation.conversation_id, key),
        ).fetchone()
        if row:
            if (row["kind"], row["content"], row["source"]) != (
                kind,
                encoded_content,
                encoded_source,
            ):
                raise ContextError("上下文幂等键已用于不同内容。")
            return row["id"]
        entry_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO context_entries(id,conversation_id,kind,content,source,"
            "idempotency_key,turn_id) VALUES(?,?,?,?,?,?,?)",
            (
                entry_id,
                conversation.conversation_id,
                kind,
                encoded_content,
                encoded_source,
                key,
                turn_id,
            ),
        )
        return entry_id

    def append(self, *args: Any) -> str:
        with self.lock, self.db:
            return self._append(*args)

    def _validate_entries(
        self, conversation: ConversationRef, entry_ids: list[str]
    ) -> None:
        self._validate(conversation)
        for entry_id in entry_ids:
            if not self.db.execute(
                "SELECT 1 FROM context_entries WHERE id=? AND conversation_id=?",
                (entry_id, conversation.conversation_id),
            ).fetchone():
                raise ContextError("消息不能关联其他会话的内容。")

    def _bind(
        self,
        conversation: ConversationRef,
        message_id: str,
        content: Any,
        sender_name: str,
        sender_id: str,
        entry_ids: list[str],
    ) -> None:
        self._validate_entries(conversation, entry_ids)
        old = self.db.execute(
            "SELECT conversation_id FROM message_bindings WHERE scope=? AND message_id=?",
            (conversation.scope.key, message_id),
        ).fetchone()
        if old:
            if old[0] != conversation.conversation_id:
                raise ContextError("消息已经属于另一个会话。")
            return
        self.db.execute(
            "INSERT INTO message_bindings VALUES(?,?,?,?,?,?,?)",
            (
                conversation.scope.key,
                message_id,
                conversation.conversation_id,
                encode(content),
                sender_name,
                sender_id,
                encode(entry_ids),
            ),
        )

    def bind(self, *args: Any) -> None:
        with self.lock, self.db:
            self._bind(*args)

    def binding(self, scope: Scope, message_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM message_bindings WHERE scope=? AND message_id=?",
                (scope.key, message_id),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            result["conversation"] = self._conversation(
                self.db.execute(
                    "SELECT * FROM conversations WHERE id=?",
                    (row["conversation_id"],),
                ).fetchone()
            )
            return result

    def request(self, scope: Scope, message_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM turns WHERE scope=? AND message_id=?",
                (scope.key, message_id),
            ).fetchone()
            return dict(row) if row else None

    def begin(
        self,
        conversation: ConversationRef,
        message_id: str,
        content: Any,
        sender_name: str,
        sender_id: str,
        activate: bool,
        control: bool,
    ) -> str:
        with self.lock, self.db:
            self._validate(conversation)
            turn_id = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO turns(id,scope,message_id,conversation_id,status) VALUES(?,?,?,?,?)",
                (
                    turn_id,
                    conversation.scope.key,
                    message_id,
                    conversation.conversation_id,
                    "control" if control else "running",
                ),
            )
            if activate:
                self._activate(conversation)
            entry_ids = []
            if not control:
                items = [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"user(name={sender_name},qq={sender_id}):",
                            },
                            *content,
                        ],
                    }
                ]
                entry_ids.append(
                    self._append(conversation, "turn", items, {}, turn_id, turn_id)
                )
            self._bind(
                conversation, message_id, content, sender_name, sender_id, entry_ids
            )
            return turn_id

    def set_input(self, conversation: ConversationRef, turn_id: str, items: Any) -> str:
        with self.lock, self.db:
            self._validate(conversation)
            row = self.db.execute(
                "SELECT id FROM context_entries WHERE conversation_id=? AND turn_id=?",
                (conversation.conversation_id, turn_id),
            ).fetchone()
            if row is None:
                raise ContextError("找不到当前轮次的输入。")
            entry_id = row[0]
            self.db.execute(
                "UPDATE context_entries SET content=? WHERE id=?",
                (encode(items), entry_id),
            )
            return entry_id

    def set_reference(
        self, conversation: ConversationRef, turn_id: str, reference: Any
    ) -> None:
        with self.lock, self.db:
            self._validate(conversation)
            self.db.execute(
                "UPDATE context_entries SET source=? WHERE conversation_id=? AND turn_id=?",
                (
                    encode({"reference": reference}),
                    conversation.conversation_id,
                    turn_id,
                ),
            )

    def entries(
        self, conversation: ConversationRef, exclude_turn: str = ""
    ) -> list[dict[str, Any]]:
        with self.lock:
            self._validate(conversation)
            rows = self.db.execute(
                "SELECT e.*, t.status AS turn_status FROM context_entries e "
                "LEFT JOIN turns t ON e.turn_id=t.id WHERE e.conversation_id=? "
                "AND COALESCE(e.turn_id,'') != ? ORDER BY seq",
                (conversation.conversation_id, exclude_turn or "-"),
            ).fetchall()
            results = []
            for row in rows:
                entry = dict(row)
                entry["content"] = json.loads(entry["content"])
                entry["source"] = json.loads(entry["source"])
                source_turn = entry["source"].get("turn_id")
                if source_turn:
                    owner = self.db.execute(
                        "SELECT status FROM turns WHERE id=?", (source_turn,)
                    ).fetchone()
                    if owner and owner[0] == "complete":
                        # The successful tool chain already describes this side effect.
                        entry["skip_projection"] = True
                if row["kind"] == "turn" and row["turn_status"] in (
                    "failed",
                    "interrupted",
                ):
                    entry["content"].append(
                        {"role": "user", "content": "[该轮回复失败或中断]"}
                    )
                delivery = self.db.execute(
                    "SELECT status FROM deliveries WHERE conversation_id=? "
                    "AND (turn_id=? OR entry_ids LIKE ?) ORDER BY rowid DESC LIMIT 1",
                    (
                        conversation.conversation_id,
                        row["turn_id"],
                        '%"' + row["id"] + '"%',
                    ),
                ).fetchone()
                entry["delivery_status"] = delivery[0] if delivery else None
                results.append(entry)
            return results

    def _prepare_delivery(
        self,
        conversation: ConversationRef,
        key: str,
        payload: Any,
        visible: Any,
        entry_ids: list[str],
        turn_id: str | None,
    ) -> dict[str, Any]:
        self._validate_entries(conversation, entry_ids)
        row = self.db.execute(
            "SELECT * FROM deliveries WHERE conversation_id=? AND idempotency_key=?",
            (conversation.conversation_id, key),
        ).fetchone()
        if row:
            if row["payload"] != encode(payload) or row["visible"] != encode(visible):
                raise ContextError("发送幂等键已用于不同消息。")
            return dict(row)
        delivery_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO deliveries(id,conversation_id,idempotency_key,payload,visible,"
            "entry_ids,turn_id,status) VALUES(?,?,?,?,?,?,?,'pending')",
            (
                delivery_id,
                conversation.conversation_id,
                key,
                encode(payload),
                encode(visible),
                encode(entry_ids),
                turn_id,
            ),
        )
        return dict(
            self.db.execute(
                "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
        )

    def prepare_delivery(self, *args: Any) -> dict[str, Any]:
        with self.lock, self.db:
            return self._prepare_delivery(*args)

    def complete(
        self,
        conversation: ConversationRef,
        turn_id: str,
        items: Any,
        payload: Any,
        visible: Any,
        entry_id: str,
    ) -> dict[str, Any]:
        with self.lock, self.db:
            self._validate_entries(conversation, [entry_id])
            self.db.execute(
                "UPDATE context_entries SET content=? WHERE id=?",
                (encode(items), entry_id),
            )
            self.db.execute("UPDATE turns SET status='complete' WHERE id=?", (turn_id,))
            return self._prepare_delivery(
                conversation, f"turn:{turn_id}", payload, visible, [entry_id], turn_id
            )

    def fail(self, turn_id: str, error: str) -> None:
        with self.lock, self.db:
            self.db.execute(
                "UPDATE turns SET status='failed',error=? WHERE id=?", (error, turn_id)
            )

    def delivery(self, delivery_id: str) -> dict[str, Any]:
        with self.lock:
            return dict(
                self.db.execute(
                    "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
                ).fetchone()
            )

    def delivery_status(self, delivery_id: str, status: str, error: str = "") -> None:
        with self.lock, self.db:
            self.db.execute(
                "UPDATE deliveries SET status=?,error=? WHERE id=?",
                (status, error, delivery_id),
            )

    def confirm(
        self, conversation: ConversationRef, delivery_id: str, message_id: str | None
    ) -> None:
        with self.lock, self.db:
            row = self.delivery(delivery_id)
            if row["conversation_id"] != conversation.conversation_id:
                raise ContextError("发送回执属于其他会话。")
            if message_id is not None:
                self._bind(
                    conversation,
                    message_id,
                    json.loads(row["visible"]),
                    "Sunny",
                    conversation.scope.bot_id,
                    json.loads(row["entry_ids"]),
                )
            self.db.execute(
                "UPDATE deliveries SET status='confirmed',receipt=?,error='' WHERE id=?",
                (message_id, delivery_id),
            )

    def pending_deliveries(
        self, bot_id: str
    ) -> list[tuple[ConversationRef, dict[str, Any]]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id, conversation_id FROM deliveries WHERE status='pending'",
            ).fetchall()
            result = []
            for row in rows:
                conversation = self._conversation(
                    self.db.execute(
                        "SELECT * FROM conversations WHERE id=?",
                        (row["conversation_id"],),
                    ).fetchone()
                )
                if conversation.scope.bot_id == bot_id:
                    result.append((conversation, self.delivery(row["id"])))
            return result

    def publication_delivery(
        self, scope: Scope, source_key: str, key: str
    ) -> dict[str, Any] | None:
        """Internal delivery dedup across fallback bot accounts, never a history read API."""
        with self.lock:
            rows = self.db.execute(
                "SELECT d.*, c.scope AS target_scope FROM deliveries d JOIN conversations c "
                "ON d.conversation_id=c.id WHERE c.source_key=? AND d.idempotency_key=? "
                "AND d.status IN ('confirmed','sending','unknown') ORDER BY d.rowid DESC",
                (source_key, key),
            ).fetchall()
            for row in rows:
                adapter, _, chat_type, peer_id = json.loads(row["target_scope"])
                if (adapter, chat_type, peer_id) == (
                    scope.adapter,
                    scope.chat_type,
                    scope.peer_id,
                ):
                    return dict(row)
            return None
