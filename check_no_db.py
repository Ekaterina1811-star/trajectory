"""Проверки проекта без базы данных: импорты и чистая логика в памяти.

Запуск из каталога проекта:

    python check_no_db.py

Что проверяется (ни одна проверка не обращается к db.session):

1. Импорт всех модулей проекта: models, abstract_customer, amocrm, main, app.
2. Маппинг-функции abstract_customer.py: customer_row / lead_row /
   contact_row / company_row.
3. Загрузчик Customer.get_schema(many=True) из amocrm.py — построение
   Customer из сырого dict ответа amoCRM.
4. Логика _DbMaps из main.py: трекинг логина, неоднозначность _AMBIGUOUS
   при логине у двух агрегатов, merge / resolve, цепочки слияний,
   защита от цикла.
5. Сценарии _handle_lead из main.py с подменой writer._ids на фейковый
   счётчик id (вместо sequence БД):
   - сделка по id CRM -> свой агрегат;
   - сделка по логину среди покупателей;
   - две разные записи -> слияние в найденную по сделке
     (merged_into == {loser: winner});
   - ничего не нашлось -> новый агрегат;
   - контакты сделки пишутся и дедупятся, связь (lead_id, contact_id)
     добавляется.

Формат вывода: [OK]/[FAIL] на каждую проверку; в конце либо
«ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ», либо список провалов и код завершения 1.
"""

import importlib
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

failures: list[str] = []


def check(name: str, fn: Callable[[], Any]) -> None:
    """Выполнить одну проверку; провал поймать, запомнить и показать."""
    try:
        fn()
    except Exception:
        failures.append(name)
        print(f"[FAIL] {name}")
        traceback.print_exc()
        return
    print(f"[OK] {name}")


# ---------------------------------------------------------------------------
# Фабрики тестовых данных (форматы — как в ответе API amoCRM)
# ---------------------------------------------------------------------------

def make_customer(customer_id: int, name: str, login: str | None,
                  embedded: dict[str, Any] | None = None):
    """Customer, собранный вручную (все поля конструктора из amocrm.py)."""
    from amocrm import Customer

    return Customer(
        id=customer_id,
        name=name,
        next_price=1500,
        next_date=1767225600,
        responsible_user_id=7,
        status_id=4242,
        periodicity=30,
        created_by=1,
        updated_by=2,
        created_at=1700000000,
        updated_at=1700000123,
        closest_task_at=None,
        is_deleted=False,
        ltv=9000,
        purchases_count=3,
        average_check=3000,
        account_id=42,
        custom_fields_values={"Логин": {"id": 1, "value": login}},
        _embedded=embedded,
    )


def make_lead(lead_id: int, name: str, login: str | None = None,
              embedded: dict[str, Any] | None = None):
    """Lead, собранный вручную (все поля конструктора из amocrm.py)."""
    from amocrm import Lead

    return Lead(
        name=name,
        id=lead_id,
        price=5000,
        responsible_user_id=7,
        group_id=None,
        status_id=553,
        pipeline_id=1,
        source_id=None,
        created_by=1,
        updated_by=2,
        created_at=1700000000,
        updated_at=1700000456,
        closest_task_at=None,
        is_deleted=False,
        score=None,
        account_id=42,
        custom_fields_values=(
            {"Логин": {"id": 1, "value": login}} if login else {}),
        _embedded=embedded,
    )


def raw_customer(customer_id: int, login: str,
                 embedded: dict[str, Any] | None = None) -> dict[str, Any]:
    """Сырой dict покупателя, как приходит из API amoCRM."""
    return {
        "id": customer_id,
        "name": f"Покупатель {customer_id}",
        "next_price": 1500,
        "next_date": 1767225600,
        "responsible_user_id": 7,
        "status_id": 4242,
        "periodicity": 30,
        "created_by": 1,
        "updated_by": 2,
        "created_at": 1700000000,
        "updated_at": 1700000123,
        "closest_task_at": None,
        "is_deleted": False,
        "ltv": 9000,
        "purchases_count": 3,
        "average_check": 3000,
        "account_id": 42,
        "custom_fields_values": {"Логин": {"id": 1, "value": login}},
        "_embedded": embedded,
    }


class FakeIdSequence:
    """Замена _AbstractIdSequence: счётчик в памяти вместо sequence БД."""

    def __init__(self, start: int = 1000):
        self._next = start

    def next_id(self) -> int:
        value = self._next
        self._next += 1
        return value


def make_writer():
    """_BatchWriter с фейковым счётчиком id — new_abstract_id() без БД."""
    import main

    writer = main._BatchWriter()
    writer._ids = FakeIdSequence()  # type: ignore[assignment]
    return writer


# ---------------------------------------------------------------------------
# 1. Импорты всех модулей проекта
# ---------------------------------------------------------------------------

def check_imports() -> None:
    modules = ("models", "abstract_customer", "amocrm", "main", "app")
    for module_name in modules:
        check(
            f"импорт: {module_name}",
            lambda name=module_name: importlib.import_module(name),
        )


# ---------------------------------------------------------------------------
# 2. Маппинг-функции abstract_customer.py
# ---------------------------------------------------------------------------

def check_customer_row() -> None:
    from abstract_customer import customer_row

    contact = {"id": 900, "name": "Контакт Ивана"}
    company = {"id": 800, "name": "ООО Ромашка"}
    from amocrm import COMPANIES, CONTACTS
    customer = make_customer(
        11, "Иван", "ivan",
        embedded={CONTACTS: [contact], COMPANIES: [company]},
    )
    row = customer_row(customer)
    assert row["crm_id"] == 11, row
    assert row["login"] == "ivan", row
    assert row["name"] == "Иван", row
    api = row["api_value"]
    assert api["id"] == 11, api
    assert api["name"] == "Иван", api
    expected = {"id": 1, "value": "ivan"}
    assert api["custom_fields_values"]["Логин"] == expected, api


def check_customer_row_without_login() -> None:
    from abstract_customer import customer_row

    row = customer_row(make_customer(12, "Без логина", login=None))
    assert row["login"] is None, row
    assert row["crm_id"] == 12, row


def check_lead_row() -> None:
    from abstract_customer import lead_row

    lead = make_lead(21, "Сделка Олега", login="oleg")
    row = lead_row(lead)
    assert row["crm_id"] == 21, row
    assert row["login"] == "oleg", row
    assert row["name"] == "Сделка Олега", row
    api = row["api_value"]
    assert api["id"] == 21, api
    assert api["custom_fields_values"]["Логин"]["value"] == "oleg", api


def check_contact_row() -> None:
    from abstract_customer import contact_row

    raw = {"id": 501, "name": "Контакт Иванова"}
    row = contact_row(raw)
    assert row["crm_id"] == 501, row
    assert row["name"] == "Контакт Иванова", row
    assert row["api_value"] == raw, row


def check_company_row() -> None:
    from abstract_customer import company_row

    raw = {"id": 800, "name": "ООО Ромашка"}
    row = company_row(raw)
    assert row == {"crm_id": 800, "name": "ООО Ромашка", "api_value": raw}, row


# ---------------------------------------------------------------------------
# 3. Загрузчик Customer.get_schema(many=True) из amocrm.py
# ---------------------------------------------------------------------------

def check_customer_schema_many() -> None:
    from amocrm import COMPANIES, CONTACTS, Customer

    contact = {"id": 900, "name": "Контакт Ивана"}
    company = {"id": 800, "name": "ООО Ромашка"}
    raw_ivan = raw_customer(
        11, "ivan", embedded={CONTACTS: [contact], COMPANIES: [company]})
    raw_petr = raw_customer(12, "petr", embedded=None)

    loaded = Customer.get_schema(many=True).load([raw_ivan, raw_petr])

    assert isinstance(loaded, list), type(loaded)
    assert len(loaded) == 2, loaded
    ivan, petr = loaded
    assert isinstance(ivan, Customer), type(ivan)
    assert isinstance(petr, Customer), type(petr)
    assert ivan.id == 11 and ivan.name == "Покупатель 11", ivan
    assert ivan.get_field("Логин") == "ivan", ivan.custom_fields_values
    assert ivan.custom_fields_values["Логин"] == {"id": 1, "value": "ivan"}
    assert ivan.contacts == [contact], ivan.contacts
    assert ivan.companies == [company], ivan.companies
    assert ivan.ltv == 9000 and ivan.purchases_count == 3, ivan.as_dict()
    assert petr.id == 12 and petr.get_field("Логин") == "petr", petr
    assert petr.contacts == [] and petr.companies == [], petr


# ---------------------------------------------------------------------------
# 4. Логика _DbMaps из main.py
# ---------------------------------------------------------------------------

def check_dbmaps_track_login() -> None:
    import main

    maps = main._DbMaps()
    maps.track_lead_login("ivan", 1)
    assert maps.by_lead_login["ivan"] == 1, maps.by_lead_login
    maps.track_lead_login("ivan", 1)  # тот же агрегат — не неоднозначность
    assert maps.by_lead_login["ivan"] == 1, maps.by_lead_login
    maps.track_lead_login("ivan", 2)  # другой агрегат -> _AMBIGUOUS
    assert maps.by_lead_login["ivan"] is main._AMBIGUOUS, maps.by_lead_login
    maps.track_lead_login("ivan", 3)  # после _AMBIGUOUS остаётся _AMBIGUOUS
    assert maps.by_lead_login["ivan"] is main._AMBIGUOUS, maps.by_lead_login

    maps.track_customer_login("anna", 5)
    maps.track_customer_login("anna", 6)
    assert maps.by_customer_login["anna"] is main._AMBIGUOUS
    maps.track_customer_login("bella", 7)
    assert maps.by_customer_login["bella"] == 7


def check_dbmaps_empty_login() -> None:
    import main

    maps = main._DbMaps()
    maps.track_lead_login(None, 1)  # type: ignore[arg-type]
    maps.track_lead_login("", 2)
    maps.track_customer_login(None, 3)  # type: ignore[arg-type]
    maps.track_customer_login("", 4)
    assert not maps.by_lead_login, maps.by_lead_login
    assert not maps.by_customer_login, maps.by_customer_login


def check_dbmaps_merge_resolve() -> None:
    import main

    maps = main._DbMaps()
    winner = maps.merge(loser=2, winner=1)
    assert winner == 1, winner
    assert maps.merged_into == {2: 1}, maps.merged_into
    assert maps.resolve(2) == 1, maps.merged_into
    assert maps.resolve(1) == 1, maps.merged_into  # победитель — сам себе


def check_dbmaps_merge_chain() -> None:
    import main

    maps = main._DbMaps()
    maps.merge(loser=2, winner=1)   # 2 -> 1
    maps.merge(loser=3, winner=2)   # winner резолвится до корня: 3 -> 1
    assert maps.merged_into == {2: 1, 3: 1}, maps.merged_into
    assert maps.resolve(3) == 1, maps.merged_into

    # переходная цепочка 3 -> 2 -> 1: resolve идёт до конца
    chained = main._DbMaps()
    chained.merged_into = {3: 2, 2: 1}
    assert chained.resolve(3) == 1, chained.merged_into
    assert chained.resolve(2) == 1, chained.merged_into


def check_dbmaps_merge_same_noop() -> None:
    import main

    maps = main._DbMaps()
    maps.merge(loser=5, winner=5)
    assert maps.merged_into == {}, maps.merged_into
    assert maps.resolve(5) == 5


def check_dbmaps_cycle_guard() -> None:
    import main

    maps = main._DbMaps()
    maps.merged_into = {1: 2, 2: 1}  # цикл 1 <-> 2
    try:
        maps.resolve(1)
    except RuntimeError:
        pass
    else:
        raise AssertionError("resolve(1) должен упасть на цикле слияний")


# ---------------------------------------------------------------------------
# 5. Сценарии _handle_lead из main.py (writer._ids подменён на фейковый)
# ---------------------------------------------------------------------------

def check_handle_lead_by_crm_id() -> None:
    import main

    maps = main._DbMaps()
    maps.lead_crm[100] = 5
    writer = make_writer()
    seen = main._SeenEntities()

    main._handle_lead(make_lead(100, "Сделка по id CRM"), maps, writer, seen)

    assert len(writer._leads) == 1, writer._leads
    row = writer._leads[0]
    assert row["crm_id"] == 100, row
    assert row["abstract_customer_id"] == 5, row
    assert writer._abstracts == [], writer._abstracts  # новый агрегат не нужен
    assert maps.lead_crm[100] == 5, maps.lead_crm


def check_handle_lead_by_login() -> None:
    import main

    maps = main._DbMaps()
    maps.by_customer_login["ivan"] = 7
    writer = make_writer()
    seen = main._SeenEntities()

    main._handle_lead(
        make_lead(200, "Сделка по логину", login="ivan"), maps, writer, seen)

    assert len(writer._leads) == 1, writer._leads
    assert writer._leads[0]["abstract_customer_id"] == 7, writer._leads[0]
    assert writer._abstracts == [], writer._abstracts
    assert maps.lead_crm[200] == 7, maps.lead_crm
    assert maps.by_lead_login["ivan"] == 7, maps.by_lead_login


def check_handle_lead_merge() -> None:
    import main

    maps = main._DbMaps()
    maps.lead_crm[300] = 9               # агрегат, найденный по сделке
    maps.by_customer_login["oleg"] = 11  # агрегат по логину покупателя
    writer = make_writer()
    seen = main._SeenEntities()

    main._handle_lead(
        make_lead(300, "Сделка с дублем", login="oleg"), maps, writer, seen)

    assert maps.merged_into == {11: 9}, maps.merged_into  # loser -> winner
    assert maps.resolve(11) == 9, maps.merged_into
    assert len(writer._leads) == 1, writer._leads
    assert writer._leads[0]["abstract_customer_id"] == 9, writer._leads[0]
    assert writer._abstracts == [], writer._abstracts
    assert maps.lead_crm[300] == 9, maps.lead_crm


def check_handle_lead_new_abstract() -> None:
    import main

    maps = main._DbMaps()  # пустые индексы: ничего не нашлось
    writer = make_writer()  # фейковый счётчик начнёт со 1000
    seen = main._SeenEntities()

    main._handle_lead(
        make_lead(400, "Совершенно новая сделка", login="petr"),
        maps, writer, seen)

    assert writer._abstracts == [{"id": 1000}], writer._abstracts
    assert len(writer._leads) == 1, writer._leads
    assert writer._leads[0]["abstract_customer_id"] == 1000, writer._leads[0]
    assert maps.lead_crm[400] == 1000, maps.lead_crm
    assert maps.by_lead_login["petr"] == 1000, maps.by_lead_login


def check_handle_lead_contacts_dedup() -> None:
    import main
    from amocrm import CONTACTS

    maps = main._DbMaps()
    writer = make_writer()
    seen = main._SeenEntities()

    lead_a = make_lead(
        500, "Сделка А", login="anna",
        embedded={CONTACTS: [
            {"id": 501, "name": "Контакт 1"},
            {"id": 502, "name": "Контакт 2"},
        ]})
    main._handle_lead(lead_a, maps, writer, seen)

    # оба контакта записаны, связи (lead_id, contact_id) добавлены
    contact_ids = [c["crm_id"] for c in writer._contacts]
    assert contact_ids == [501, 502], writer._contacts
    assert set(writer._lead_contacts) == {(500, 501), (500, 502)}, \
        writer._lead_contacts

    lead_b = make_lead(
        600, "Сделка Б", login="bella",
        embedded={CONTACTS: [
            {"id": 501, "name": "Контакт 1"},   # уже видели в этой выгрузке
            {"id": 503, "name": "Контакт 3"},
        ]})
    main._handle_lead(lead_b, maps, writer, seen)

    # контакт 501 не дублируется, новые контакт и связь добавлены
    assert [c["crm_id"] for c in writer._contacts] == [501, 502, 503], \
        writer._contacts
    assert set(writer._lead_contacts) == {
        (500, 501), (500, 502), (600, 501), (600, 503)}, writer._lead_contacts


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def run() -> int:
    print("Проверки без БД: синхронизация amoCRM -> Postgres (вариант B)")
    print()

    check_imports()

    check("customer_row: колонки + api_value", check_customer_row)
    check("customer_row: без логина", check_customer_row_without_login)
    check("lead_row: колонки + api_value", check_lead_row)
    check("contact_row: колонки + api_value", check_contact_row)
    check("company_row: колонки + api_value", check_company_row)

    check("Customer.get_schema(many=True): строит Customer из сырых dict",
          check_customer_schema_many)

    check("_DbMaps: трекинг логина, _AMBIGUOUS при двух агрегатах",
          check_dbmaps_track_login)
    check("_DbMaps: пустой логин не трекается", check_dbmaps_empty_login)
    check("_DbMaps: merge/resolve", check_dbmaps_merge_resolve)
    check("_DbMaps: цепочки слияний", check_dbmaps_merge_chain)
    check("_DbMaps: merge сам с собой — пусто", check_dbmaps_merge_same_noop)
    check("_DbMaps: защита от цикла слияний", check_dbmaps_cycle_guard)

    check("_handle_lead: сделка по id CRM -> свой агрегат",
          check_handle_lead_by_crm_id)
    check("_handle_lead: сделка по логину среди покупателей",
          check_handle_lead_by_login)
    check("_handle_lead: две записи -> слияние в найденную по сделке",
          check_handle_lead_merge)
    check("_handle_lead: ничего не нашлось -> новый агрегат",
          check_handle_lead_new_abstract)
    check("_handle_lead: контакты пишутся и дедупятся",
          check_handle_lead_contacts_dedup)

    print()
    if failures:
        print(f"ПРОВАЛЕНО ПРОВЕРОК: {len(failures)}")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(run())
