"""Local authoritative chart preferences in the existing memory durability boundary."""

import hashlib
import hmac
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from pydantic import Field, model_validator

from visualization_runtime.contracts import Contract
from visualization_runtime.paths import confined_path
from visualization_runtime.snapshots import digest
from visualization_runtime.store import JobConflict, canonical

KEYS = {
    "palette",
    "font_family",
    "font_size",
    "density",
    "legend_position",
    "units",
    "altitude_unit",
    "chart_style",
    "special_needs",
}
SECRET = re.compile(r"(?i)(sk-[\w-]{12,}|api[_ -]?key|password|passwd|密码|密钥|bearer\s+\S+)")
FUTURE = re.compile(r"(?i)(以后|今后|所有图|长期|请记住|记住我的|always\s+use|remember\s+my)")
LOCAL = re.compile(r"(?i)(这张|本图|这次|仅本次|only this|不要记住|别记住|不用记住|不要保存)")


class MemoryToken(Contract):
    epoch: int = Field(ge=1)
    revision: int = Field(ge=0)


class PreferenceEdit(Contract):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    epoch: int = Field(ge=1)
    revision: int = Field(ge=0)
    key: str
    value: str | int | None
    task_type: str = Field(
        default="all", pattern=r"^(all|general|trajectory|prediction|classification)$"
    )

    @model_validator(mode="after")
    def valid(self):
        validate(self.key, self.value)
        return self


def validate(key, value):
    if key not in KEYS and not re.fullmatch(r"rule_[a-z0-9_]{1,48}", key):
        raise ValueError("unknown_chart_preference")
    if value is None:
        return
    if key == "font_size":
        if type(value) is not int or not 8 <= value <= 40:
            raise ValueError("invalid_chart_font_size")
    elif (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > (300 if key == "special_needs" else 80)
        or SECRET.search(value)
    ):
        raise ValueError("invalid_chart_preference_value")


def extract(text, *, durable=False):
    if not isinstance(text, str) or len(text) > 8000 or SECRET.search(text):
        return {}
    inherited = {}
    if durable:
        masked = re.sub(
            r'“[^”]*”|‘[^’]*’|"[^"]*"|\x27[^\x27]*\x27|`[^`]*`',
            lambda match: " " * len(match[0]),
            text,
        )
        future = FUTURE.search(masked)
        if future is None or re.search(r"示例|假设|引用|解释|什么意思", masked[: future.start()]):
            return {}
        text = text[future.start() :]
        if re.search(r"不要记住|别记住|不用记住|不要保存", text):
            return {}
        local = LOCAL.search(text)
        if local:
            text = text[: local.start()]
    else:
        local = re.search(r"这张|本图|这次|仅本次|only this", text, re.IGNORECASE)
        if local:
            inherited = extract(text, durable=True)
            text = text[local.start() :]
            future = FUTURE.search(text)
            if future:
                text = text[: future.start()]
    if durable and not re.search(
        r"图表|绘图|图例|字号|字体|布局|单位|轨迹图|航迹图|预测图|分类图|折线图|柱状图|散点图|热力图|(?:用|采用|配色|偏好|喜欢).{0,6}(?:蓝色|红色|绿色|暖色|冷色|色盲)|(?:用|使用|改用|采用).{0,4}(?:公里|海里|英尺|宋体|黑体|微软雅黑|Arial)",
        text,
    ):
        return {}
    found = {}
    avoided = []

    def negated(word):
        return re.search(
            r"(?:不要|不用|不想|别用|不使用|不喜欢|不偏好).{0,3}" + re.escape(word), text
        )

    for word, value in (
        ("蓝色", "blue"),
        ("红色", "red"),
        ("绿色", "green"),
        ("暖色", "warm"),
        ("冷色", "cool"),
        ("色盲友好", "colorblind_safe"),
    ):
        if word in text and not negated(word):
            found["palette"] = value
        elif word in text:
            avoided.append((word, value))
    if avoided and "palette" not in found:
        found["palette"] = "avoid_" + avoided[0][1]
    size = re.search(
        r"(?:字号|字体大小|font size)\s*(?:设为|改成|用|为|[:：=])?\s*(\d{1,2})",
        text,
        re.IGNORECASE,
    )
    if size and 8 <= int(size[1]) <= 40 and not negated("字号") and not negated("字体大小"):
        found["font_size"] = int(size[1])
    for word, value in (
        ("宋体", "SimSun"),
        ("黑体", "SimHei"),
        ("微软雅黑", "Microsoft YaHei"),
        ("Arial", "Arial"),
    ):
        if (
            word in text
            and not negated(word)
            and (word != "黑体" or "黑体辐射" not in text or "字体" in text)
        ):
            found["font_family"] = value
    legend = re.search(r"图例.{0,8}(顶部|上方|底部|下方|左侧|右侧)", text)
    if legend and not negated("图例") and not negated(legend[1]):
        found["legend_position"] = {
            "顶部": "top",
            "上方": "top",
            "底部": "bottom",
            "下方": "bottom",
            "左侧": "left",
            "右侧": "right",
        }[legend[1]]
    for word, value in (("紧凑布局", "compact"), ("宽松布局", "spacious"), ("默认密度", "normal")):
        if word in text and not negated(word):
            found["density"] = value
    for word, value in (("公里", "km"), ("海里", "nmi"), ("英尺", "ft")):
        if re.search(r"(?:单位|使用|改用|用).{0,5}" + word, text) and not negated(word):
            found["units"] = value
    for word, value in (
        ("折线图", "line"),
        ("柱状图", "bar"),
        ("散点图", "scatter"),
        ("热力图", "heatmap"),
    ):
        if word in text and not negated(word):
            found["chart_style"] = value
    needs = re.search(r"图表特殊需求[:：]\s*([^\n]{1,300})", text)
    if needs:
        found["special_needs"] = needs[1].strip()
    elif re.search(r"网格|留白|标签|旋转|对比度|无障碍|线条|背景", text):
        for clause in re.split(r"[。；;\n]", text):
            if re.search(r"图表|本图|这张图|轨迹图|预测图", clause) and re.search(
                r"网格|留白|标签|旋转|对比度|无障碍|线条|背景", clause
            ):
                found["special_needs"] = clause.strip()[:300]
                break
    if avoided:
        found["special_needs"] = (
            found.get("special_needs", "") + "；避免使用" + "、".join(word for word, _ in avoided)
        ).strip("；")[:300]
    return inherited | found


def scope(text):
    return (
        "trajectory"
        if "轨迹图" in text or "航迹图" in text
        else "prediction"
        if "预测图" in text
        else "classification"
        if "分类图" in text
        else "all"
    )


def migrate(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS memory_outbox(id TEXT PRIMARY KEY,owner TEXT NOT NULL,epoch INTEGER NOT NULL,workspace TEXT NOT NULL,session TEXT NOT NULL,content TEXT NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS memory_users(owner TEXT PRIMARY KEY, epoch INTEGER NOT NULL, enabled INTEGER NOT NULL, updated REAL NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS chart_memory_state(owner TEXT PRIMARY KEY,revision INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS chart_request_versions(id TEXT PRIMARY KEY,owner TEXT NOT NULL,revision INTEGER NOT NULL)"
    )
    db.execute("INSERT OR IGNORE INTO chart_memory_state SELECT owner,0 FROM memory_users")
    db.execute("""CREATE TABLE IF NOT EXISTS chart_preferences(
        id TEXT PRIMARY KEY,owner TEXT NOT NULL,epoch INTEGER NOT NULL,task_type TEXT NOT NULL,
        key TEXT NOT NULL,value TEXT,evidence TEXT NOT NULL,source_artifact TEXT,
        confidence TEXT NOT NULL,state TEXT NOT NULL,updated REAL NOT NULL,supersedes TEXT)""")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_chart_preference_active ON chart_preferences(owner,epoch,task_type,key) WHERE state='active'"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS chart_preference_events(owner TEXT NOT NULL,request_id TEXT NOT NULL,epoch INTEGER NOT NULL,input_sha TEXT NOT NULL,PRIMARY KEY(owner,request_id))"
    )
    db.execute("""CREATE TRIGGER IF NOT EXISTS chart_user_insert AFTER INSERT ON memory_users
        BEGIN INSERT OR IGNORE INTO chart_memory_state VALUES(NEW.owner,0); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS chart_user_toggle AFTER UPDATE OF epoch,enabled ON memory_users
        WHEN OLD.epoch!=NEW.epoch OR OLD.enabled!=NEW.enabled
        BEGIN INSERT OR IGNORE INTO chart_memory_state VALUES(NEW.owner,0);
        UPDATE chart_memory_state SET revision=revision+1 WHERE owner=NEW.owner; END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS chart_user_forget AFTER UPDATE OF epoch ON memory_users
        WHEN OLD.epoch!=NEW.epoch BEGIN
        DELETE FROM chart_preferences WHERE owner=NEW.owner;
        DELETE FROM chart_preference_events WHERE owner=NEW.owner;
        DELETE FROM chart_request_versions WHERE owner=NEW.owner; END""")


class ChartPreferences:
    def __init__(self, path, identity, *, root, max_pending=1000):
        self.root, self.identity = Path(root).resolve(), identity
        self.path = confined_path(self.root, Path(path))
        self.max_pending = max_pending
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            migrate(db)
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def owner(self, user):
        return hmac.new(self.identity.encode(), user.encode(), hashlib.sha256).hexdigest()

    def user(self, db, owner, prefix=""):
        db.execute(
            f"INSERT OR IGNORE INTO {prefix}memory_users(owner,epoch,enabled,updated) VALUES(?,1,1,?)",
            (owner, time.time()),
        )
        db.execute(f"INSERT OR IGNORE INTO {prefix}chart_memory_state VALUES(?,0)", (owner,))
        return self.read_user(db, owner, prefix)

    @staticmethod
    def read_user(db, owner, prefix=""):
        row = db.execute(
            f"SELECT u.*,s.revision AS chart_revision FROM {prefix}memory_users u JOIN {prefix}chart_memory_state s USING(owner) WHERE u.owner=?",
            (owner,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def token(user):
        return {"epoch": user["epoch"], "revision": user["chart_revision"]}

    def put_in(
        self,
        db,
        owner,
        user,
        values,
        *,
        request_id,
        evidence,
        task_type="all",
        source=None,
        prefix="",
        enqueue=True,
    ):
        raw = digest(
            {"values": values, "task_type": task_type, "source": source, "evidence": evidence}
        )
        old = db.execute(
            f"SELECT * FROM {prefix}chart_preference_events WHERE owner=? AND request_id=?",
            (owner, request_id),
        ).fetchone()
        if old:
            if old["epoch"] != user["epoch"] or old["input_sha"] != raw:
                raise ValueError("chart_preference_request_conflict")
            return False
        for key, value in values.items():
            validate(key, value)
            prior = db.execute(
                f"SELECT id FROM {prefix}chart_preferences WHERE owner=? AND epoch=? AND task_type=? AND key=? AND state='active'",
                (owner, user["epoch"], task_type, key),
            ).fetchone()
            db.execute(
                f"UPDATE {prefix}chart_preferences SET state='superseded' WHERE owner=? AND epoch=? AND task_type=? AND key=? AND state='active'",
                (owner, user["epoch"], task_type, key),
            )
            if value is None:
                quotes = [
                    r[0]
                    for r in db.execute(
                        f"SELECT DISTINCT evidence FROM {prefix}chart_preferences WHERE owner=? AND epoch=? AND task_type=? AND key=? AND evidence!=''",
                        (owner, user["epoch"], task_type, key),
                    )
                ]
                for quote in quotes:
                    db.execute(
                        f"UPDATE {prefix}chart_preferences SET evidence='' WHERE owner=? AND evidence=?",
                        (owner, quote),
                    )
                    db.execute(
                        f"UPDATE {prefix}memory_outbox SET content='',state='cancelled' WHERE owner=? AND kind!='delete' AND content=?",
                        (owner, quote),
                    )
                db.execute(
                    f"UPDATE {prefix}memory_outbox SET content='',state='cancelled' WHERE owner=? AND session='chart-preferences'",
                    (owner,),
                )
                if db.execute(
                    f"SELECT 1 FROM {prefix}sqlite_master WHERE name='memory_requests'"
                ).fetchone():
                    db.execute(
                        f"UPDATE {prefix}memory_requests SET instructions='',content=NULL,state=CASE WHEN state='prepared' THEN 'revoked' ELSE state END WHERE owner=?",
                        (owner,),
                    )
                db.execute(
                    f"UPDATE {prefix}chart_preferences SET value=NULL,evidence='',state='deleted' WHERE owner=? AND epoch=? AND task_type=? AND key=?",
                    (owner, user["epoch"], task_type, key),
                )
            else:
                db.execute(
                    f"INSERT INTO {prefix}chart_preferences VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        uuid4().hex,
                        owner,
                        user["epoch"],
                        task_type,
                        key,
                        canonical(value),
                        evidence[:4000],
                        source,
                        "explicit",
                        "active",
                        time.time(),
                        prior[0] if prior else None,
                    ),
                )
        db.execute(
            f"INSERT INTO {prefix}chart_preference_events VALUES(?,?,?,?)",
            (owner, request_id, user["epoch"], raw),
        )
        db.execute(
            f"UPDATE {prefix}chart_memory_state SET revision=revision+1 WHERE owner=?",
            (owner,),
        )
        db.execute(f"UPDATE {prefix}memory_users SET updated=? WHERE owner=?", (time.time(), owner))
        if (
            enqueue
            and user["enabled"]
            and db.execute(
                f"SELECT count(*) FROM {prefix}memory_outbox WHERE state='pending'"
            ).fetchone()[0]
            < self.max_pending
        ):
            db.execute(
                f"INSERT OR IGNORE INTO {prefix}memory_outbox VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "chart:" + owner + ":" + request_id,
                    owner,
                    user["epoch"],
                    "scat-" + owner + "-" + str(user["epoch"]),
                    "chart-preferences",
                    "用户明确更新图表偏好，以本地结构化记录为准："
                    + canonical({"task_type": task_type, "values": values}),
                    "correction",
                    "pending",
                    time.time(),
                ),
            )
        return True

    def effective(self, db, owner, user, task_type, *, prefix=""):
        if not user["enabled"]:
            return {}
        rows = db.execute(
            f"SELECT key,value FROM {prefix}chart_preferences WHERE owner=? AND epoch=? AND state='active' AND task_type IN ('all',?) ORDER BY CASE WHEN task_type='all' THEN 0 ELSE 1 END",
            (owner, user["epoch"], task_type),
        ).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def list(self, user_id):
        owner = self.owner(user_id)
        with closing(self.connect()) as db, db:
            user = self.user(db, owner)
            rows = db.execute(
                "SELECT id,task_type,key,value,evidence,source_artifact,confidence,updated,supersedes FROM chart_preferences WHERE owner=? AND epoch=? AND state='active' ORDER BY task_type,key",
                (owner, user["epoch"]),
            ).fetchall()
        return {
            "enabled": bool(user["enabled"]),
            **self.token(user),
            "items": [dict(r) | {"value": json.loads(r["value"])} for r in rows],
        }

    def edit(self, user_id, body):
        owner = self.owner(user_id)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            user = self.user(db, owner)
            old = db.execute(
                "SELECT * FROM chart_preference_events WHERE owner=? AND request_id=?",
                (owner, "edit:" + body.request_id),
            ).fetchone()
            if not old and self.token(user) != {"epoch": body.epoch, "revision": body.revision}:
                raise ValueError("chart_preferences_changed")
            if not user["enabled"] and body.value is not None:
                raise ValueError("memory_disabled")
            self.put_in(
                db,
                owner,
                user,
                {body.key: body.value},
                request_id="edit:" + body.request_id,
                evidence="用户在记忆页明确设置",
                task_type=body.task_type,
            )
        return self.list(user_id)

    def queue_withdrawals(self, db, owner, user):
        rows = db.execute(
            "SELECT DISTINCT p.task_type,p.key FROM chart_preferences p WHERE p.owner=? AND p.epoch=? AND p.state='deleted' AND NOT EXISTS (SELECT 1 FROM chart_preferences a WHERE a.owner=p.owner AND a.epoch=p.epoch AND a.task_type=p.task_type AND a.key=p.key AND a.state='active')",
            (owner, user["epoch"]),
        ).fetchall()
        if (
            not rows
            or db.execute("SELECT count(*) FROM memory_outbox WHERE state='pending'").fetchone()[0]
            >= self.max_pending
        ):
            return
        db.execute(
            "INSERT OR IGNORE INTO memory_outbox VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "chart-resume:"
                + owner
                + ":"
                + str(user["epoch"])
                + ":"
                + str(user["chart_revision"]),
                owner,
                user["epoch"],
                "scat-" + owner + "-" + str(user["epoch"]),
                "chart-preferences",
                "用户已撤回这些图表偏好，不再从旧摘要恢复；以本地记录为准："
                + canonical([dict(r) for r in rows]),
                "correction",
                "pending",
                time.time(),
            ),
        )


def configured(root):
    from hermes_runtime.autonomy_config import load_autonomy, private_text

    policy = load_autonomy(root)
    if policy is None or not policy.memory.enabled:
        return None
    return ChartPreferences(
        root / policy.memory.state,
        private_text(root, policy.memory.identity_key),
        root=root,
        max_pending=policy.memory.max_pending,
    )


def assert_current(db, jobs, job):
    binding = db.execute(
        "SELECT * FROM visualization_preference_bindings WHERE task_id=?", (job["task_id"],)
    ).fetchone()
    if binding is None:
        return
    path = confined_path(jobs.workspace_root, Path(binding["path"]))
    if not path.is_file():
        raise JobConflict("visualization_preferences_unavailable")
    if not any(r[1] == "vis_preferences" for r in db.execute("PRAGMA database_list")):
        db.execute("ATTACH DATABASE ? AS vis_preferences", (str(path),))
    from visualization_runtime.persistence import require_atomic_journals

    require_atomic_journals(db, "vis_preferences")
    user = db.execute(
        "SELECT u.epoch,s.revision,u.enabled FROM vis_preferences.memory_users u JOIN vis_preferences.chart_memory_state s USING(owner) WHERE u.owner=?",
        (binding["owner_hash"],),
    ).fetchone()
    if user is None or tuple(user) != (binding["epoch"], binding["revision"], binding["enabled"]):
        raise JobConflict("visualization_preferences_changed")


def task_type(snapshot):
    names = " ".join(d["operation"] for d in snapshot["payload"]["datasets"])
    return (
        "prediction"
        if "predict" in names or "forecast" in names
        else "trajectory"
        if "track" in names or "trajectory" in names
        else "classification"
        if "classif" in names
        else "general"
    )


def setup(
    store,
    context,
    local,
    kind,
    *,
    text=None,
    token=None,
    source=None,
    request_id=None,
    semantic=False,
    input_text=None,
    reference_source=None,
):
    def apply(db, task_id, request):
        if not any(r[1] == "vis_preferences" for r in db.execute("PRAGMA database_list")):
            db.execute("ATTACH DATABASE ? AS vis_preferences", (str(store.path),))
        from visualization_runtime.persistence import require_atomic_journals

        require_atomic_journals(db, "vis_preferences")
        owner = store.owner(context.user_id)
        user = store.user(db, owner, "vis_preferences.")
        previous = db.execute(
            "SELECT learned FROM visualization_preference_bindings WHERE task_id=?", (task_id,)
        ).fetchone()
        learned = bool(previous[0]) if previous and text is None and input_text is None else False
        values = extract(text, durable=True) if text and not semantic else {}
        if values and token is not None:
            if store.token(user) != token:
                raise JobConflict("visualization_memory_changed")
            if user["enabled"]:
                learned = store.put_in(
                    db,
                    owner,
                    user,
                    values,
                    request_id="visualization:" + context.scope_key + ":" + request_id,
                    evidence=text,
                    task_type=scope(text),
                    source=source,
                    prefix="vis_preferences.",
                )
                user = store.user(db, owner, "vis_preferences.")
        effective = store.effective(db, owner, user, kind, prefix="vis_preferences.") | local
        if len(effective) > 32:
            raise JobConflict("visualization_preference_capacity_exceeded")
        if semantic and input_text is not None:
            saved_records = (
                [
                    dict(row) | {"value": json.loads(row["value"])}
                    for row in db.execute(
                        "SELECT task_type,key,value FROM vis_preferences.chart_preferences WHERE owner=? AND epoch=? AND state='active' ORDER BY task_type,key LIMIT 64",
                        (owner, user["epoch"]),
                    )
                ]
                if user["enabled"]
                else []
            )
            db.execute(
                "INSERT INTO visualization_preference_inputs VALUES(?,?,?,?,?,?,?,'pending',NULL,NULL,NULL)",
                (
                    task_id,
                    input_text,
                    canonical(
                        {
                            "intent": request.intent,
                            "preferences": effective,
                            "task_type": kind,
                            "saved_records": saved_records,
                            "reference_source": reference_source[:18000]
                            if reference_source
                            else None,
                        }
                    ),
                    token["epoch"] if token else user["epoch"],
                    token["revision"] if token else user["chart_revision"],
                    int(text is not None and token is not None),
                    source,
                ),
            )
        db.execute(
            "INSERT OR REPLACE INTO visualization_preference_bindings VALUES(?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                str(store.path),
                owner,
                user["epoch"],
                user["chart_revision"],
                user["enabled"],
                canonical(local),
                kind,
                int(learned),
            ),
        )
        return request.model_copy(
            update={"preferences": effective, "preferences_sha256": digest(effective)}
        )

    return apply


def refresh(app, context, task_id, worker, generation):
    with app.jobs.transaction() as db:
        row = app.jobs._fence(db, task_id, worker, generation)
        request = app.jobs._decode(row)["request"]
        binding = db.execute(
            "SELECT * FROM visualization_preference_bindings WHERE task_id=?", (task_id,)
        ).fetchone()
        if app.preferences is None and binding is None:
            return False
        local = json.loads(binding["local_json"]) if binding else extract(request.intent)
        kind = binding["task_type"] if binding else "general"
        if app.preferences:
            updated = setup(app.preferences, context, local, kind)(db, task_id, request)
            new_binding = db.execute(
                "SELECT * FROM visualization_preference_bindings WHERE task_id=?", (task_id,)
            ).fetchone()
            changed = (
                binding is None
                or any(
                    binding[k] != new_binding[k] for k in ("path", "epoch", "revision", "enabled")
                )
                or updated.preferences_sha256 != request.preferences_sha256
            )
        else:
            db.execute("DELETE FROM visualization_preference_bindings WHERE task_id=?", (task_id,))
            updated = request.model_copy(
                update={"preferences": local, "preferences_sha256": digest(local)}
            )
            changed = True
        if not changed:
            return False
        if db.execute(
            "SELECT 1 FROM visualization_resources WHERE task_id=? AND state='active'", (task_id,)
        ).fetchone():
            raise JobConflict("visualization_preference_cleanup_required")
        if db.execute(
            "SELECT 1 FROM visualization_provider_calls WHERE task_id=? AND state='sending'",
            (task_id,),
        ).fetchone():
            raise JobConflict("visualization_preferences_provider_inflight")
        db.execute(
            "UPDATE visualization_provider_calls SET state='aborted',charged_tokens=0,finished=?,error_code='preferences_changed' WHERE task_id=? AND state='reserved'",
            (app.jobs.clock(), task_id),
        )
        updated = updated.model_copy(update={"preferences_round": request.preferences_round + 1})
        raw = canonical(updated.model_dump(mode="json"))
        db.execute(
            "UPDATE visualization_jobs SET request=?,request_sha=?,phase='generating',updated=? WHERE task_id=?",
            (raw, hashlib.sha256(raw.encode()).hexdigest(), app.jobs.clock(), task_id),
        )
        app.jobs._event(db, row, "preferences_refreshed")
        return True
