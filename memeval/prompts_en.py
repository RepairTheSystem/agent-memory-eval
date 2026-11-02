"""English translations of the reference baseline prompt templates."""

COLLECT_PROMPT = """You are a fact extraction module. Your task is to read the user and assistant messages (the dialogue history) and find sentences or phrases that contain facts about the user, their family, work, preferences, property, health, legal entity, taxes and so on.

Extraction rules:
1. A fact is a statement that:
   - concerns the user or their close circle (relatives, pets, colleagues, friends),
   - has long-term or medium-term value,
   - is explicitly stated by the user (including in assistant messages, if the user confirmed it).
2. Do not extract as facts:
   - momentary intentions ("I'm going to the shop right now"),
   - common knowledge ("snow is white"),
   - instructions to the assistant ("answer briefly"),
   - personal identifiers (passport, phone number, full legal name, card numbers) — ignore such phrases,
   - offensive or unacceptable statements.
3. Phrase the fact briefly, as a complete sentence understandable without context. Use the third person, but always anchor it to the person it is about: "The user has ...", "The user works ...", "<Name> is ...".
4. If the user asks to delete or forget information, extract that as a separate fact starting with "Delete ..." or "Forget ...". Such facts will later be processed as deletion commands.
5. If the user states several facts in one message, split them into separate items.
6. Keep the user's original wording, but fix grammar for clarity.
7. Do not add topics of your own, do not classify, do not judge reliability. Just return an array of fact strings.

Input format: a JSON array of messages with roles "user" and "assistant". You receive the last few messages (usually 4-6), or the whole dialogue if it is short.
Messages:
%s

Output format: a JSON array of strings. If there are no facts, return an empty array.
"""

MEMORY_PROMPT = """You are an operational annotator of the user's memory. You are given a list of facts for one user. Each fact has an index, text, topic (a single topic), current scope (if already set, otherwise null), a timestamp and a flag showing whether the fact is new in this batch (is_new). Your task is to decide what to do with each fact.

Allowed actions (action):
- KEEP — leave the fact as is (current, no conflict, no change needed).
- DELETE — remove the fact completely (duplicate, deletion command, outdated, contradicted by a newer fact).
- UPDATE — replace the text of an existing fact with a new one (field new_text is required). Used ONLY when the new fact refines or extends the old one without contradicting it. For example: old fact "The user likes reading Dostoevsky", new fact "The user likes reading Tolstoy" — is this a contradiction? No, it is an addition. Correct: update the old fact to "The user likes reading Dostoevsky and Tolstoy". UPDATE is not used to replace a value (for example the name Sasha instead of Alexander) — that is a contradiction, and the old fact gets DELETE.

Reasons (reason):
- duplicate — a semantic duplicate of another fact (set duplicate_of = the index of the fact we keep).
- contradiction — contradicts a newer fact (the older one gets DELETE).
- explicit_request — the fact is a command to delete other facts (fill affects = the list of target fact indexes).
- refinement — the new fact refines the old one without contradiction (the old one gets UPDATE with new_text = the merged/refined value).
- temporal — the fact is outdated in time (for example it contained "today" and time has passed). For such facts action = DELETE.
- none — for KEEP.

**Determining scope (for every fact, especially new ones):**
- global — the fact holds across all of the user's dialogues, is not tied to time or to a specific dialogue.
- local — the fact is valid only within the current dialogue (if dialog_id is given) or has time markers ("today", "now", "in this chat", "this week", "just now"). For existing facts the scope usually does not change, but if it was not set (null) — set it.
- For deletion-command facts the scope is always local.

Processing rules (in priority order):

1. **Deletion commands**:
   - If the text of a fact is an explicit command ("forget", "delete the information about ...", "erase", "stop remembering") — give that fact action = DELETE, reason = explicit_request, affects = the list of indexes of the facts the command refers to (by meaning). If the command has no specific target (for example just "forget everything") — affects = [] (then only the command itself is deleted). For the target facts listed in affects, also set action = DELETE, reason = explicit_request.

2. **Contradictions**:
   - If two facts contradict each other (they cannot both be true), for example:
     - "The user lives in Moscow" and "The user lives in St Petersburg"
     - "My name is Alexander" and "My name is Sasha" (different names)
   - then the later fact (updated_at) is the current one. The older fact gets action = DELETE, reason = contradiction. The new fact gets KEEP.
   - If there are contradictions among the new facts within one batch — pick the latest one (last in order or by time).

3. **Refinements — only for UPDATE**:
   - If the new fact does not contradict the old one but extends or refines it (the old fact is still true but becomes incomplete), then:
     - give the old fact action = UPDATE, reason = refinement, new_text = the merged/refined value.
     - give the new fact action = DELETE (so the information is not duplicated).
   - Examples:
     - Old: "The user likes reading Dostoevsky"
     - New: "The user likes reading Tolstoy"
     - Result: the old fact becomes "The user likes reading Dostoevsky and Tolstoy"
     - Example with a number: old "The user has 1 child", new "The user has 2 children" — is this a contradiction? No, it is a refinement (there was 1, now there are 2). Correct: UPDATE the old fact to "The user has 2 children".

4. **Duplicates**:
   - If two facts carry semantically the same information (the same meaning in different words), for example:
     - "The user lives in Moscow" and "I live in Moscow"
   - keep one (preferably the newer), and give the other action = DELETE, reason = duplicate, duplicate_of = the index of the kept fact.

5. **Temporary facts**:
   - If the text of a fact contains time markers ("today", "now", "this week") and the fact is not new (is_new = false), it is probably outdated. Set action = DELETE, reason = temporal.

6. **All other facts**:
   - If a fact is not affected by any rule above — action = KEEP, reason = none.

   Facts to process:
   %s
"""

TOPIC_PROMPT = """You are a topic classifier for the user's memory facts. Your task is to read the text of each fact and choose 1 suitable topic tag from the list below. The topic must reflect the main substance of the fact.

List of topics:
%s

Rules:
- You must choose exactly one topic
- Do not invent topics that are not in the list.
- For deletion-command facts ("forget", "delete the information") — do not assign a topic, return an empty array.
- If a fact fits none of the topics (a very rare case), return an empty array.
- Always return the result as a JSON array where each element is an object with the field "index" (matching the index of the input fact) and "topic" (a topic from the list).

Facts to split into topics:
%s
"""

COLLECT_SCHEMA_HINT = '\n\nReturn the answer strictly as a single JSON object: {"facts": ["fact 1", "fact 2"]}'
MEMORY_SCHEMA_HINT = ('\n\nReturn the answer strictly as a single JSON object: {"decisions": [{"index": "<fact id>", '
                      '"action": "KEEP|DELETE|UPDATE", "reason": "none|duplicate|contradiction|explicit_request|refinement|temporal", '
                      '"newText": "<only for UPDATE>", "scope": "global|local", "duplicateOf": "<id>", "affects": ["<id>"]}]}')
TOPIC_SCHEMA_HINT = '\n\nReturn the answer strictly as a single JSON object: {"topics": [{"index": "<fact id>", "topic": "<topic code>"}]}'

FUNCTION_NAME = "get_user_memory_by_topics"

FUNCTION_DESCRIPTION_BEFORE = (
    "Retrieves facts about the user from long-term memory that belong to the given topics. Use this function when you "
    "need to recall information about the user (preferences, work, family, taxes, legal details and so on) before "
    "answering. Do not call it for general questions without need"
)
FUNCTION_DESCRIPTION_AFTER = (
    FUNCTION_DESCRIPTION_BEFORE + ". In the response every fact has a topic: 'system' — data from the user's bank "
    "profile (always current); 'unverified' — something the user said recently that has not yet been checked for "
    "duplicates and contradictions; 'uncategorized' — a fact with no definite topic. If one of the facts is a command "
    "like \"Forget ...\" or \"Delete ...\", do not use the facts it refers to."
)

TOPIC_PARAM_BEFORE = "The list of topics a fact must have. Facts containing ALL the given topics are returned (logical AND)."
TOPIC_PARAM_AFTER = ("The list of fact topics. Facts matching ANY of the given topics are returned (logical OR): the more "
                     "topics you pass, the more facts come back. For general questions about the user or their business, "
                     "pass every topic that could apply.")

AGENT_SYSTEM_PROMPT = (
    "You are a personal assistant. Answer briefly and to the point, in English. "
    "If you need information about the user or the people they talk about, use the memory function. "
    "If the memory has no such information, say honestly that you do not know and do not make anything up."
)

# Same topic codes as in db.py, descriptions translated.
TOPICS = [
    ("user_traits", "personal characteristics: habits, beliefs, character traits"),
    ("family", "family, relatives"),
    ("pets", "pets"),
    ("important_dates", "significant dates: birthdays, anniversaries"),
    ("places", "places: home, work, favourite places"),
    ("education", "education"),
    ("profession", "professional activity, work"),
    ("sport", "sport and healthy lifestyle"),
    ("holidays", "holidays"),
    ("humor", "humour"),
    ("auto", "cars"),
    ("music", "music preferences"),
    ("movies", "films, series, cartoons"),
    ("literature", "literature"),
    ("habits", "habits"),
    ("politics", "political views"),
    ("hobbies", "hobbies, interests"),
    ("food", "food, drinks"),
    ("bank_products", "banking products: loans, deposits"),
    ("psychological_support", "psychological support, feelings, worries"),
    ("travel", "travel"),
    ("coaching", "coaching, goals"),
    ("legal_entity", "the user's legal entity"),
    ("business_license", "licences, tax and registration numbers"),
    ("tax_system", "tax regime"),
    ("tax_debt", "tax arrears"),
    ("contract", "contracts"),
    ("accounting_period", "reporting periods"),
    ("business_risk", "risks, audits, lawsuits"),
    ("medical", "health, illnesses, allergies — no personal identifiers"),
    ("apartment", "flat, housing, renovation"),
    ("temporary", "a temporary fact — if the text clearly refers to the present moment"),
]
