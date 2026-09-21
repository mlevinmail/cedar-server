"""Fixtures for backend/lang.py that the Gutenberg corpus can't cover.

The corpus (47k books, tools note in lang.py) measures accuracy on the five
Latin-script languages Kokoro speaks. What it can't measure is the case that
matters just as much: text in a language Kokoro has NO voice for, which must
detect as None so the account's own voice is left alone. Detecting German as
French would swap a bad reading for a worse one.

    python tools/lang_selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cedar import lang  # noqa: E402

# (label, expected, text). Expected None = "leave the account voice alone".
CASES: list[tuple[str, str | None, str]] = [
    # --- languages with a voice ------------------------------------------
    ("english", "en",
     "The car had been parked outside the house since early morning and nobody "
     "knew who it belonged to. The neighbours had talked about it the night "
     "before, but none of them would admit to having seen anything. When the "
     "sun rose higher a man came out of the house next door and said that he "
     "knew the car, though he could not remember when he had last seen it."),
    ("french", "fr",
     "La voiture était garée devant la maison depuis le petit matin et personne "
     "ne savait à qui elle appartenait. Les voisins en avaient parlé la veille, "
     "mais aucun d'entre eux ne voulait avouer avoir vu quelque chose. Quand le "
     "soleil est monté plus haut, un homme est sorti de la maison d'à côté et a "
     "dit qu'il connaissait cette voiture, sans se rappeler quand il l'avait vue."),
    ("spanish", "es",
     "El coche estaba aparcado delante de la casa desde primera hora de la "
     "mañana y nadie sabía de quién era. Los vecinos habían hablado de ello la "
     "noche anterior, pero ninguno quería admitir que hubiera visto algo. "
     "Cuando el sol subió más alto, un hombre salió de la casa de al lado y "
     "dijo que conocía el coche, aunque no recordaba cuándo lo había visto."),
    ("italian", "it",
     "L'automobile era parcheggiata davanti alla casa dal primo mattino e "
     "nessuno sapeva a chi appartenesse. I vicini ne avevano parlato la sera "
     "prima, ma nessuno di loro voleva ammettere di aver visto qualcosa. "
     "Quando il sole è salito più in alto, un uomo è uscito dalla casa accanto "
     "e ha detto che conosceva quella macchina, ma non ricordava quando."),
    ("portuguese", "pt",
     "O carro estava estacionado em frente à casa desde o início da manhã e "
     "ninguém sabia de quem era. Os vizinhos já tinham falado sobre isso na "
     "noite anterior, mas nenhum deles queria admitir que tinha visto alguma "
     "coisa. Quando o sol subiu mais alto, um homem saiu da casa ao lado e "
     "disse que conhecia o carro, mas não se lembrava de quando o tinha visto."),
    ("japanese", "ja",
     "私は昨日の夜、駅の近くにある小さな本屋に入った。棚には古い雑誌が並んでいて、"
     "店主は静かに新聞を読んでいた。しばらく本を眺めてから、一冊だけ買って外に出た。"
     "外はまだ雨が降っていて、傘を持っていないことに気がついた。"),
    ("chinese", "zh",
     "他昨天晚上走进了车站附近的一家小书店。书架上摆着旧杂志，店主安静地读着报纸，"
     "没有抬头看我。我看了一会儿书，只买了一本就走出去了。外面还在下雨，"
     "我这才想起自己没有带伞。"),
    ("hindi", "hi",
     "मैं कल रात स्टेशन के पास एक छोटी सी किताब की दुकान में गया। वहाँ पुरानी "
     "पत्रिकाएँ रखी थीं और दुकानदार चुपचाप अखबार पढ़ रहा था। कुछ देर किताबें देखने "
     "के बाद मैंने सिर्फ एक किताब खरीदी और बाहर निकल आया। बाहर अब भी बारिश हो रही थी।"),

    # --- languages with no voice: must not be pinned ----------------------
    ("german", None,
     "Der Wagen stand seit dem frühen Morgen vor dem Haus, und niemand wusste, "
     "wem er gehörte. Die Nachbarn hatten schon am Abend darüber gesprochen, "
     "aber keiner wollte etwas gesehen haben. Als die Sonne höher stieg, kam "
     "ein Mann aus dem Nebenhaus und sagte, dass er den Wagen kenne, doch er "
     "könne sich nicht erinnern, wann er ihn zuletzt gesehen habe."),
    ("dutch", None,
     "De man liep langs de gracht en keek naar de boten die daar lagen. Het was "
     "koud, maar de zon scheen door de wolken heen en dat maakte de wandeling "
     "aangenaam. Hij dacht aan zijn werk en aan alles wat hij nog moest doen "
     "voordat de week voorbij was. In de verte hoorde hij een klok slaan."),
    ("polish", None,
     "Samochód stał przed domem od wczesnego ranka i nikt nie wiedział, do kogo "
     "należy. Sąsiedzi rozmawiali o tym poprzedniego wieczoru, ale żaden z nich "
     "nie chciał przyznać, że cokolwiek widział. Kiedy słońce wzeszło wyżej, z "
     "sąsiedniego domu wyszedł mężczyzna i powiedział, że zna ten samochód."),
    ("russian", None,
     "Машина стояла перед домом с раннего утра, и никто не знал, кому она "
     "принадлежит. Соседи говорили об этом ещё накануне вечером, но никто из "
     "них не хотел признаться, что что-то видел. Когда солнце поднялось выше, "
     "из соседнего дома вышел мужчина и сказал, что знает эту машину."),
    ("ukrainian", None,
     "Автомобіль стояв перед будинком із самого ранку, і ніхто не знав, кому він "
     "належить. Сусіди говорили про це ще напередодні ввечері, але жоден із них "
     "не хотів визнати, що будь-що бачив. Коли сонце піднялося вище, із "
     "сусіднього будинку вийшов чоловік і сказав, що знає цю машину."),
    # Mostly Cyrillic with real French dialogue in it: the words alone would
    # call this French. The script rule has to say "no voice" first.
    ("russian with french dialogue", None,
     "— Eh bien, mon prince. Gênes et Lucques ne sont plus que des apanages, "
     "des поместья, de la famille Buonaparte. Non, je vous préviens que si vous "
     "ne me dites pas que nous avons la guerre, si vous vous permettez encore "
     "de pallier toutes les infamies... Так говорила в июле 1805 года известная "
     "Анна Павловна Шерер, фрейлина и приближённая императрицы Марии Феодоровны, "
     "встречая важного и чиновного князя Василия, первого приехавшего на её "
     "вечер. Анна Павловна кашляла несколько дней, у неё был грипп, как она "
     "говорила. Все её знакомые получили утром записки с красным лакеем. "
     "Dieu, quelle virulente sortie! — отвечал, нисколько не смутясь такою "
     "встречей, вошедший князь, в придворном шитом мундире, в чулках, башмаках "
     "и звёздах, с светлым выражением плоского лица."),
    ("swedish", None,
     "Bilen hade stått utanför huset sedan tidigt på morgonen och ingen visste "
     "vem den tillhörde. Grannarna hade talat om den kvällen innan, men ingen "
     "av dem ville erkänna att de hade sett något. När solen steg högre kom en "
     "man ut från huset bredvid och sade att han kände igen bilen."),
    ("latin", None,
     "Gallia est omnis divisa in partes tres, quarum unam incolunt Belgae, "
     "aliam Aquitani, tertiam qui ipsorum lingua Celtae, nostra Galli "
     "appellantur. Hi omnes lingua, institutis, legibus inter se differunt. "
     "Gallos ab Aquitanis Garumna flumen, a Belgis Matrona et Sequana dividit. "
     "Horum omnium fortissimi sunt Belgae, propterea quod a cultu atque "
     "humanitate provinciae longissime absunt."),
    ("turkish", None,
     "Araba sabahın erken saatlerinden beri evin önünde duruyordu ve kimin "
     "olduğunu kimse bilmiyordu. Komşular bir önceki akşam bunun hakkında "
     "konuşmuşlardı ama hiçbiri bir şey gördüğünü kabul etmek istemedi. Güneş "
     "yükseldiğinde yan evden bir adam çıktı ve arabayı tanıdığını söyledi."),

    # --- too little to judge: silence, not a guess ------------------------
    ("two words", None, "Hello there."),
    ("short paste", None, "Meeting at four. Bring the report and the second folder."),

    # --- a quotation must not move the whole document ---------------------
    ("english with french quote", "en",
     "The author opens the third chapter with a line he never bothers to "
     "translate for the reader — «je ne sais quoi de plus grand que nous» — and "
     "then spends the next forty pages explaining what he meant by it, which "
     "rather defeats the purpose. It is the kind of book that assumes you have "
     "read everything it has read, and forgives you if you have not, provided "
     "you are willing to pretend otherwise for the length of an afternoon."),
    ("english with chinese term", "en",
     "The concept the paper keeps returning to is written 道 in the original, "
     "and every translator since has quarrelled about it. Some render it as the "
     "way, others as the path, and a few refuse to translate it at all on the "
     "grounds that any English word would smuggle in assumptions the author did "
     "not hold. The dispute has outlived all of them and shows no sign of "
     "being settled by the present generation of scholars either."),
]


def main() -> int:
    failures = 0
    for label, want, text in CASES:
        got = lang.detect(text)
        ok = got == want
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'}  {label:28s} want={str(want):5s} got={str(got)}")

    # Which voice actually reads a document: (label, doc language, the voices
    # the user has chosen, their main voice, per-document override, expected).
    reads = [
        # Nothing chosen yet — the curated default rescues the language.
        ("fr doc, English reader", "fr", {}, "af_heart", None, "ff_siwis"),
        ("es doc, English reader", "es", {}, "af_heart", None, "ef_dora"),
        ("es doc, male reader", "es", {}, "am_michael", None, "ef_dora"),
        ("en doc, English reader", "en", {}, "af_heart", None, "af_heart"),
        ("en doc, British reader", "en", {}, "bf_emma", None, "bf_emma"),
        # The account voice is Japanese — English must not be read with it.
        ("en doc, Japanese reader", "en", {}, "jf_tebukuro", None, "af_heart"),
        ("ja doc, Japanese reader", "ja", {}, "jf_tebukuro", None, "jf_tebukuro"),
        # Once chosen, the language's voice wins over the curated default...
        ("es doc, chose Alex", "es", {"es": "em_alex"}, "af_heart", None, "em_alex"),
        ("en doc, chose Emma", "en", {"en": "bf_emma"}, "af_heart", None, "bf_emma"),
        # ...and choosing Spanish leaves English exactly where it was.
        ("en doc, chose Spanish too", "en", {"es": "em_alex", "en": "af_heart"},
         "em_alex", None, "af_heart"),
        # A language with no voice, and a document we couldn't place.
        ("de doc (no voice exists)", "de", {}, "af_heart", None, "af_heart"),
        ("undetected doc", None, {"es": "em_alex"}, "ef_dora", None, "ef_dora"),
        # A deliberate cross-language pin beats everything.
        ("pinned document", "es", {"es": "em_alex"}, "af_heart", "bf_emma", "bf_emma"),
    ]
    print()
    for label, doc_lang, chosen, main, override, want_voice in reads:
        got = lang.resolve(doc_lang, chosen, main, override)
        ok = got == want_voice
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'}  {label:28s} want={want_voice:12s} got={got}")

    # Does a pick mean "this language" or "just this document"?
    scopes = [
        ("Spanish voice on a Spanish doc", "em_alex", "es", True),
        ("English voice on an English doc", "bf_emma", "en", True),
        ("French voice on an English doc", "ff_siwis", "en", False),
        ("English voice on a Spanish doc", "af_heart", "es", False),
        ("any voice on an unplaced doc", "ff_siwis", None, True),
        ("any voice, language has no voices", "af_heart", "de", True),
    ]
    print()
    for label, voice, doc_lang, want_speaks in scopes:
        got = lang.speaks(voice, doc_lang)
        ok = got == want_speaks
        failures += not ok
        scope = "language-wide" if got else "this document"
        print(f"{'ok  ' if ok else 'FAIL'}  {label:36s} -> {scope}")

    print(f"\n{'PASS' if not failures else str(failures) + ' FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
