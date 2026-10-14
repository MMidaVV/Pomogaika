"""Библиотека ситуаций: одна на все объекты, ведём мы.

Ситуация — это повод, по которому гость пишет: «как заехать», «просит скидку»,
«код не сработал». У каждой есть шаблон ответа с подстановками вида {parking}:
факты подставляются из knowledge конкретного объекта.

Почему библиотека общая, а не у каждого админа своя: сорок вопросов на объект никто
руками не напишет, а если напишет — получится сорок разных представлений о том, можно
ли обещать возврат. Админ меняет формулировки под свой объект и добавляет свои поводы,
но политики и набор ситуаций едины.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from pomogaika.models import Scenario

# needs — какие факты объекта нужны шаблону. Если факта нет, ситуация числится
# незаполненной, и админ видит это во вкладке как дыру.
LIBRARY: list[dict] = [
    {
        "code": "checkin",
        "title": "Как заехать, где ключи",
        "question_class": "typical",
        "keywords": ["заезд", "заселен", "ключ", "как пройти", "где квартира", "подъезд", "заехать"],
        "template": "Заезд с {check_in_from}, адрес: {address}. {access}",
        "needs": ["access"],
        "sort": 10,
    },
    {
        "code": "door_code",
        "title": "Код калитки, домофона, сейфа",
        "question_class": "typical",
        "keywords": ["код", "калитк", "домофон", "не подходит", "не срабатывает", "открыть", "сейф"],
        "template": "Код одноразовый и приходит в этот чат оплаченной брони, не раньше чем за 2 часа до заезда. "
                    "Если код не сработал — напишите сразу, вышлем новый или откроем удалённо.",
        "needs": [],
        "sort": 20,
    },
    {
        "code": "wifi",
        "title": "Wi-Fi и пароль",
        "question_class": "typical",
        "keywords": ["wifi", "wi-fi", "вайфай", "вай-фай", "интернет", "пароль"],
        "template": "{wifi}",
        "needs": ["wifi"],
        "sort": 30,
    },
    {
        "code": "parking",
        "title": "Парковка, машина, шлагбаум",
        "question_class": "typical",
        "keywords": ["парков", "машин", "шлагбаум", "авто", "въезд"],
        "template": "{parking}",
        "needs": ["parking"],
        "sort": 40,
    },
    {
        "code": "checkout",
        "title": "Во сколько выезд",
        "question_class": "typical",
        "keywords": ["выезд", "выселен", "сдать", "во сколько уезжать", "освободить"],
        "template": "Выезд до {check_out_until}. Ключ оставьте там же, где брали, дверь захлопните.",
        "needs": [],
        "sort": 50,
    },
    {
        "code": "early_late",
        "title": "Ранний заезд или поздний выезд",
        "question_class": "typical",
        "keywords": ["раньше", "ранний заезд", "поздний выезд", "позже", "оставить вещи", "багаж"],
        "template": "Заезд с {check_in_from}, выезд до {check_out_until}. Раньше или позже — зависит от соседней брони, "
                    "уточню у собственника и вернусь с ответом.",
        "needs": [],
        "sort": 60,
    },
    {
        "code": "sea",
        "title": "Далеко ли море, центр, что рядом",
        "question_class": "typical",
        "keywords": ["море", "пляж", "центр", "рядом", "дойти", "далеко"],
        "template": "{around}",
        "needs": ["around"],
        "sort": 70,
    },
    {
        "code": "pets",
        "title": "Можно ли с животными",
        "question_class": "typical",
        "keywords": ["собак", "кошк", "котом", "животн", "питомц"],
        "template": "{pets_rule}",
        "needs": ["pets_rule"],
        "sort": 80,
    },
    {
        "code": "smoking_parties",
        "title": "Курение и вечеринки",
        "question_class": "typical",
        "keywords": ["курить", "курен", "вечеринк", "шумн", "праздн", "компан"],
        "template": "Курение в квартире и на балконе запрещено, вечеринки тоже. "
                    "Если нарушить — это основание удержать залог.",
        "needs": [],
        "sort": 90,
    },
    {
        "code": "guests_count",
        "title": "Сколько человек можно разместить",
        "question_class": "typical",
        "keywords": ["сколько человек", "гостей", "спальн", "кроват", "размест", "дополнительн"],
        "template": "Размещаем до {max_guests} гостей. {beds}",
        "needs": ["beds"],
        "sort": 100,
    },
    {
        "code": "household",
        "title": "Быт: техника, кондиционер, стирка",
        "question_class": "typical",
        "keywords": ["кондиционер", "стиральн", "машинк", "плита", "посуд", "утюг", "фен", "как включ"],
        "template": "{household}",
        "needs": ["household"],
        "sort": 110,
    },
    {
        "code": "transfer",
        "title": "Трансфер, как добраться",
        "question_class": "typical",
        "keywords": ["трансфер", "аэропорт", "вокзал", "встретить", "такси", "добраться"],
        "template": "{transfer}",
        "needs": ["transfer"],
        "sort": 120,
    },
    {
        "code": "deposit",
        "title": "Залог: сколько и когда вернут",
        "question_class": "money",
        "keywords": ["залог", "депозит", "заклад"],
        "template": "{deposit}",
        "needs": ["deposit"],
        "sort": 130,
    },
    {
        "code": "discount",
        "title": "Просит скидку",
        "question_class": "money",
        "keywords": ["скидк", "дешевл", "снизить", "торг", "подешевле", "уступ"],
        "template": "Цену и скидки решает собственник — передаю ему ваш вопрос и вернусь с ответом. "
                    "Сама скидку пообещать не могу.",
        "needs": [],
        "sort": 140,
    },
    {
        "code": "refund",
        "title": "Возврат, отмена, перенос дат",
        "question_class": "money",
        "keywords": ["возврат", "вернуть деньги", "отмен", "перенест", "перенос", "передумал"],
        "template": "Возвраты, отмены и перенос дат решает собственник, и делается это на площадке, "
                    "где вы бронировали. Передаю ваш вопрос и вернусь с ответом.",
        "needs": [],
        "sort": 150,
    },
    {
        "code": "broken",
        "title": "Что-то сломалось, нет воды или света",
        "question_class": "conflict",
        "keywords": ["сломал", "не работает", "потек", "нет воды", "нет света", "засор", "разбил"],
        "template": "Поняла, разбираюсь. Напишите, пожалуйста, что именно и приложите фото — "
                    "передам собственнику прямо сейчас, вернусь с решением.",
        "needs": [],
        "sort": 160,
    },
    {
        "code": "noise",
        "title": "Шум, соседи, стройка",
        "question_class": "conflict",
        "keywords": ["шум", "соседи", "громко", "стройк", "ремонт у соседей", "спать"],
        "template": "Сожалею, что так вышло. {noise} Передаю собственнику, вернусь с ответом.",
        "needs": ["noise"],
        "sort": 170,
    },
    {
        "code": "cleaning",
        "title": "Уборка, полотенца, бельё",
        "question_class": "typical",
        "keywords": ["уборк", "полотенц", "бель", "смена", "убрать", "чисто"],
        "template": "{cleaning}",
        "needs": ["cleaning"],
        "sort": 180,
    },
    {
        "code": "documents",
        "title": "Отчётные документы, чек",
        "question_class": "money",
        "keywords": ["чек", "документ", "отчётн", "квитанц", "командировк", "счёт"],
        "template": "Уточню у собственника, какие документы он может предоставить, и вернусь с ответом.",
        "needs": [],
        "sort": 190,
    },
    {
        "code": "host",
        "title": "Кто хост, как связаться",
        "question_class": "typical",
        "keywords": ["хост", "хозя", "собственник", "позвонить", "телефон", "связаться"],
        "template": "{host}",
        "needs": ["host"],
        "sort": 200,
    },
]


def seed_library(session: Session) -> int:
    """Залить библиотеку. Существующие ситуации обновляем, чужие не трогаем."""
    changed = 0
    for item in LIBRARY:
        row = session.query(Scenario).filter_by(code=item["code"]).one_or_none()
        if row is None:
            session.add(Scenario(**item))
            changed += 1
            continue
        # обновляем формулировку и ключевые слова, если библиотека поехала вперёд
        if row.template != item["template"] or row.keywords != item["keywords"]:
            row.template = item["template"]
            row.keywords = item["keywords"]
            row.title = item["title"]
            row.question_class = item["question_class"]
            row.needs = item["needs"]
            row.sort = item["sort"]
            changed += 1
    session.flush()
    return changed
