"""Turn a scene's narration text into a Pexels stock-footage search query.

These scripts are abstract (commitment, loneliness, self-worth) while stock
footage is indexed by concrete visuals, so a literal bag-of-words search
often returns nothing usable. Two passes handle that:

  1. THEME_QUERIES maps recurring emotional themes in this channel's writing
     onto queries that actually have footage behind them.
  2. Anything not caught by a theme falls back to content-word extraction.

Whatever comes out is only a starting point -- the picker UI's search box is
the real control, and every query is editable per scene.
"""
import re
import unicodedata

# Words that carry no search signal. Kept deliberately broad: a query of
# "the way that she" returns noise, "distance" returns usable footage.
STOPWORDS = {
    "en": {
        "a", "about", "actually", "after", "again", "against", "all", "almost", "already",
        "also", "always", "am", "an", "and", "another", "any", "anything", "are", "around",
        "as", "at", "away", "back", "be", "became", "because", "been", "before", "began",
        "being", "between", "both", "but", "by", "came", "can", "cannot", "come", "comes",
        "could", "did", "do", "does", "doing", "done", "down", "each", "either", "else",
        "enough", "even", "ever", "every", "everything", "few", "finally", "first", "for",
        "from", "get", "gets", "getting", "give", "gives", "go", "going", "gone", "good",
        "got", "had", "has", "have", "having", "he", "her", "here", "hers", "herself", "him",
        "himself", "his", "how", "however", "i", "if", "in", "instead", "into", "is", "it",
        "its", "itself", "just", "keep", "kind", "know", "knows", "last", "let", "like",
        "little", "long", "look", "looking", "made", "make", "makes", "making", "many",
        "may", "maybe", "me", "mean", "means", "might", "mine", "more", "most", "much",
        "must", "my", "myself", "need", "needs", "never", "new", "next", "no", "not",
        "nothing", "now", "of", "off", "often", "on", "once", "one", "only", "or", "other",
        "our", "out", "over", "own", "part", "perhaps", "put", "quite", "rather", "real",
        "really", "right", "said", "same", "say", "saying", "says", "see", "seem", "seems",
        "seen", "she", "should", "simply", "since", "so", "some", "someone", "something",
        "sometimes", "still", "such", "sure", "take", "takes", "than", "that", "the",
        "their", "theirs", "them", "themselves", "then", "there", "these", "they", "thing",
        "things", "think", "this", "those", "though", "through", "time", "to", "together",
        "too", "took", "toward", "under", "until", "up", "upon", "us", "use", "used",
        "very", "want", "wants", "was", "way", "ways", "we", "well", "went", "were", "what",
        "when", "where", "whether", "which", "while", "who", "whole", "whom", "why", "will",
        "with", "within", "without", "would", "yet", "you", "your", "yours", "yourself",
    },
    "es": {
        "a", "al", "algo", "alguien", "algun", "alguna", "algunas", "alguno", "algunos",
        "ante", "antes", "aqui", "asi", "aun", "aunque", "bien", "cada", "casi", "como",
        "con", "contra", "cual", "cuales", "cuando", "cuanto", "de", "del", "desde",
        "despues", "donde", "dos", "el", "ella", "ellas", "ellos", "en", "entonces",
        "entre", "era", "eran", "eres", "es", "esa", "esas", "ese", "eso", "esos", "esta",
        "estan", "estar", "estas", "este", "esto", "estos", "estoy", "fue", "fueron", "ha",
        "hace", "hacer", "hacia", "han", "hasta", "hay", "incluso", "la", "las", "le",
        "les", "lo", "los", "mas", "me", "mi", "mientras", "mis", "misma", "mismo", "mucho",
        "muy", "nada", "ni", "no", "nos", "nosotros", "nuestra", "nuestro", "nunca", "o",
        "otra", "otras", "otro", "otros", "para", "pero", "poco", "por", "porque", "puede",
        "pueden", "que", "quien", "quienes", "se", "sea", "ser", "si", "siempre", "sin",
        "sino", "sobre", "solo", "son", "su", "sus", "tal", "tambien", "tan", "tanto", "te",
        "tener", "tiene", "tienen", "todo", "todos", "tu", "tus", "un", "una", "uno",
        "unos", "usted", "va", "vez", "y", "ya", "yo",
    },
    "ur": {
        "آپ", "اب", "اپنا", "اپنی", "اپنے", "اس", "اسی", "اگر", "امر", "ان", "اور", "ایک",
        "بعد", "بغیر", "بلکہ", "بھی", "بہت", "پر", "پھر", "تک", "تم", "تھا", "تھی", "تھے",
        "تو", "جا", "جاتا", "جاتی", "جاتے", "جب", "جو", "جیسا", "جیسے", "حالانکہ", "دیا",
        "دے", "را", "رہا", "رہی", "رہے", "سا", "سب", "سکتا", "سکتی", "سکتے", "سے", "صرف",
        "کا", "کچھ", "کر", "کرتا", "کرتی", "کرتے", "کرنا", "کرنے", "کسی", "کہ", "کہا",
        "کوئی", "کون", "کی", "کیا", "کیسے", "کے", "کو", "گا", "گی", "گے", "گیا", "لیا",
        "لیکن", "لیے", "مگر", "میں", "نے", "نہ", "نہیں", "والا", "والی", "والے", "وہ",
        "ہر", "ہم", "ہو", "ہوا", "ہوئی", "ہوئے", "ہونا", "ہونے", "ہی", "ہے", "ہیں", "یا",
        "یہ", "یہاں", "یہی",
    },
}

# Minimum length for a word to count as a content word. Urdu is written far
# more compactly than Latin script -- "دل" (heart), "غم" (grief) and "سچ"
# (truth) are all real content words at 2-3 characters, so the Latin default
# of 4 would throw away most of the signal.
MIN_WORD_LEN = {"ur": 2}
DEFAULT_MIN_WORD_LEN = 4

# Recurring themes in this channel's writing -> queries with real footage
# behind them. Matched against the scene text as whole words, longest key
# first, so "self worth" wins over a bare "worth". Values are English on
# purpose: Pexels' index is English-language regardless of narration language.
THEME_QUERIES = [
    ("emotionally unavailable", "man looking away window rain"),
    ("mixed signals", "woman checking phone waiting"),
    ("bare minimum", "empty dinner table candle"),
    ("self respect", "woman standing confident sunrise"),
    ("self worth", "woman looking mirror morning light"),
    ("walking away", "person walking away empty road"),
    ("letting go", "hand releasing leaf wind"),
    ("moving on", "woman walking forward sunrise path"),
    ("waiting for", "woman waiting window phone"),
    ("left on read", "phone screen notification dark"),
    ("crumbs", "single crumb empty plate table"),
    ("loneliness", "silhouette alone empty room window"),
    ("lonely", "person alone bench evening"),
    ("silence", "empty quiet room morning light"),
    ("commitment", "couple holding hands close"),
    ("marriage", "wedding rings hands close"),
    ("boundaries", "closed door hallway light"),
    ("healing", "sunrise calm water peaceful"),
    ("closure", "closing door soft light"),
    ("anxiety", "restless hands close up dark"),
    ("exhausted", "tired woman resting head hands"),
    ("clarity", "clear water sunlight calm"),
    ("distance", "long empty road horizon"),
    ("trust", "two hands reaching each other"),
    ("betrayal", "broken glass floor dark"),
    ("grief", "rain window sad reflection"),
    ("hope", "sunrise through trees warm light"),
    ("patience", "slow flowing river stones"),
    ("respect", "two people talking calm sunlight"),
    ("attention", "person looking at phone ignoring"),
    ("effort", "hands working carefully close up"),
    ("love", "couple silhouette sunset warm"),
    ("peace", "calm lake mountains morning mist"),
]

_THEME_QUERIES_SORTED = sorted(THEME_QUERIES, key=lambda kv: -len(kv[0]))

# Urdu needs its own theme table: the English keys above can never match Urdu
# script, and unlike Spanish -- where a stray content word still sometimes
# lands on Pexels' index -- an Urdu word returns zero results every time. The
# values stay English because Pexels' index is English regardless of narration
# language, so this table is really "Urdu theme -> the same visual we'd pick
# for the English original".
LANG_THEME_QUERIES = {
    "ur": [
        ("عزت نفس", "woman standing confident sunrise"),
        ("خود اعتمادی", "woman standing confident sunrise"),
        ("انتظار", "woman waiting window phone"),
        ("تنہائی", "silhouette alone empty room window"),
        ("اکیلا", "person alone bench evening"),
        ("تنہا", "person alone bench evening"),
        ("خاموشی", "empty quiet room morning light"),
        ("وابستگی", "couple holding hands close"),
        ("شادی", "wedding rings hands close"),
        ("حدود", "closed door hallway light"),
        ("دروازہ", "closing door soft light"),
        ("بےچینی", "restless hands close up dark"),
        ("بے چینی", "restless hands close up dark"),
        ("تھکن", "tired woman resting head hands"),
        ("فاصلہ", "long empty road horizon"),
        ("بھروسا", "two hands reaching each other"),
        ("اعتماد", "two hands reaching each other"),
        ("دھوکہ", "broken glass floor dark"),
        ("آنسو", "rain window sad reflection"),
        ("بارش", "rain window sad reflection"),
        ("امید", "sunrise through trees warm light"),
        ("صبر", "slow flowing river stones"),
        ("عزت", "two people talking calm sunlight"),
        ("توجہ", "person looking at phone ignoring"),
        ("فون", "phone screen notification dark"),
        ("محبت", "couple silhouette sunset warm"),
        ("سکون", "calm lake mountains morning mist"),
        ("آئینہ", "woman looking mirror morning light"),
        ("راستہ", "person walking away empty road"),
        ("سفر", "person walking forward sunrise path"),
        ("روشنی", "sunrise calm water peaceful"),
        ("اندھیرا", "silhouette alone empty room window"),
        ("معافی", "hand releasing leaf wind"),
        ("رشتہ", "couple holding hands close"),
        ("الفاظ", "empty quiet room morning light"),
        ("خواب", "calm cinematic clouds soft light"),
        ("دل", "candle flame close up dark"),
        ("غم", "rain window sad reflection"),
    ],
}

_LANG_THEME_QUERIES_SORTED = {
    lang: sorted(pairs, key=lambda kv: -len(kv[0]))
    for lang, pairs in LANG_THEME_QUERIES.items()
}

# Languages whose words Pexels cannot search at all -- for these, a scene that
# matches no theme gets the generic query rather than a guaranteed-empty
# search in the local script.
NON_SEARCHABLE_SCRIPTS = {"ur"}

# Fallback when a scene has no usable content words at all (e.g. a scene made
# entirely of stopwords, which happens on short connective passages).
GENERIC_QUERY = "calm cinematic nature soft light"

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _fold_accents(text):
    """Strip accents so Spanish scene text matches the unaccented stopword
    list ('mas' vs 'más'). Only used for comparison, never for output."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def extract_keywords(text, lang="en", max_words=5):
    """Content words from a scene, in order of appearance, longest first.

    Length is a crude proxy for specificity, but it reliably prefers
    "disappointment" over "felt" without needing a POS tagger, and the
    picker UI makes any bad pick a one-field fix.
    """
    stop = STOPWORDS.get(lang, STOPWORDS["en"])
    min_len = MIN_WORD_LEN.get(lang, DEFAULT_MIN_WORD_LEN)
    seen = set()
    candidates = []
    for match in _WORD_RE.finditer(text.lower()):
        word = match.group(0)
        folded = _fold_accents(word)
        if len(folded) < min_len or folded in stop or folded in seen:
            continue
        seen.add(folded)
        candidates.append(word)
    candidates.sort(key=len, reverse=True)
    return candidates[:max_words]


def build_query(text, lang="en", max_words=5):
    """Best-effort Pexels query for a scene: a matched theme if the scene
    hits one, otherwise its content words."""
    haystack = " " + _fold_accents(text.lower()) + " "
    themes = _LANG_THEME_QUERIES_SORTED.get(lang, []) + _THEME_QUERIES_SORTED
    for theme, query in themes:
        if re.search(r"\b" + re.escape(theme) + r"\b", haystack):
            return query
    if lang in NON_SEARCHABLE_SCRIPTS:
        return GENERIC_QUERY
    words = extract_keywords(text, lang=lang, max_words=max_words)
    return " ".join(words) if words else GENERIC_QUERY
