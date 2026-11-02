"""In-memory SQLite tables and topic taxonomy for the reference baseline."""
import sqlite3
import uuid

# Темы классификатора; те же коды используются в enum функции чтения.
TOPICS = [
    ("user_traits", "личные характеристики: привычки, убеждения, особенности характера"),
    ("family", "семья, родственники"),
    ("pets", "домашние животные"),
    ("important_dates", "значимые даты: дни рождения, годовщины"),
    ("places", "места: жильё, работа, любимые места"),
    ("education", "образование"),
    ("profession", "профессиональная деятельность"),
    ("sport", "спорт и ЗОЖ"),
    ("holidays", "праздники"),
    ("humor", "юмор"),
    ("auto", "автомобили"),
    ("music", "музыкальные предпочтения"),
    ("movies", "кино, сериалы, мультфильмы"),
    ("literature", "литература"),
    ("habits", "привычки"),
    ("politics", "политические взгляды"),
    ("hobbies", "хобби, увлечения"),
    ("food", "еда, напитки"),
    ("bank_products", "банковские продукты: кредиты, вклады"),
    ("psychological_support", "психологическая поддержка, переживания"),
    ("travel", "путешествия"),
    ("coaching", "коучинг, цели"),
    ("legal_entity", "юридическое лицо пользователя"),
    ("business_license", "лицензии, ИНН, ОГРН"),
    ("tax_system", "налоговая система"),
    ("tax_debt", "задолженности по налогам"),
    ("contract", "договоры"),
    ("accounting_period", "отчётные периоды"),
    ("business_risk", "риски, проверки, суды"),
    ("medical", "здоровье, заболевания, аллергии — без ПДн"),
    ("apartment", "квартира, жильё, ремонт"),
    ("temporary", "временный факт — если текст содержит явные указания на текущий момент"),
]

SCHEMA = """
create table topic (
    guid text primary key,
    code text unique not null,
    description text
);
create table memory (
    guid text primary key,
    digital_user_id text not null,
    dialog_guid text,
    topic_guid text references topic(guid),
    type text,
    status text not null,
    fact text not null,
    insert_date real not null,
    update_date real not null
);
"""


def new_id() -> str:
    return str(uuid.uuid4())


class MemoryDB:
    def __init__(self, topics=None):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for code, descr in (topics or TOPICS):
            self.conn.execute("insert into topic(guid, code, description) values (?,?,?)", (new_id(), code, descr))

    def snapshot(self):
        """Независимая копия базы (для параллельных вопросов к одному и тому же состоянию памяти)."""
        dst = MemoryDB.__new__(MemoryDB)
        dst.conn = sqlite3.connect(":memory:", check_same_thread=False)
        dst.conn.row_factory = sqlite3.Row
        dst.conn.executescript("".join(self.conn.iterdump()))
        return dst

    # ---- topic ----
    def topics(self):
        return [dict(r) for r in self.conn.execute("select * from topic order by rowid")]

    def topic_codes(self):
        return [t["code"] for t in self.topics()]

    def topic_guid(self, code):
        r = self.conn.execute("select guid from topic where code=?", (code,)).fetchone()
        return r["guid"] if r else None

    # ---- memory ----
    def insert_fact(self, user, fact, *, status, now, dialog=None, type_=None, topic_code=None, guid=None):
        guid = guid or new_id()
        self.conn.execute(
            "insert into memory values (?,?,?,?,?,?,?,?,?)",
            (guid, user, dialog, self.topic_guid(topic_code) if topic_code else None, type_, status, fact, now, now),
        )
        return guid

    def user_facts(self, user):
        """Все факты пользователя с кодом топика (аналог findByDigitalUserIdWithTopic)."""
        rows = self.conn.execute(
            """select m.*, t.code as topic_code from memory m left join topic t on t.guid = m.topic_guid
               where m.digital_user_id = ? order by m.insert_date, m.rowid""",
            (user,),
        )
        return [dict(r) for r in rows]

    def exists_status(self, user, status):
        return self.conn.execute(
            "select 1 from memory where digital_user_id=? and status=? limit 1", (user, status)
        ).fetchone() is not None

    def write_fact(self, f):
        """Записать изменённый факт (UPDATE по guid). Если строки уже нет — ничего не делает."""
        self.conn.execute(
            "update memory set type=?, status=?, fact=?, topic_guid=?, update_date=? where guid=?",
            (f["type"], f["status"], f["fact"], f["topic_guid"], f["update_date"], f["guid"]),
        )

    def row_exists(self, guid):
        return self.conn.execute("select 1 from memory where guid=?", (guid,)).fetchone() is not None

    def delete_fact(self, guid):
        self.conn.execute("delete from memory where guid=?", (guid,))
