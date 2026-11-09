import logging
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from abstract_customer import company_row, contact_row, customer_row, lead_row
from amocrm import COMPANIES, CONTACTS, Customer, Lead, amocrm_get
from models import (
    AbstractCustomer,
    AmoCompany,
    AmoContact,
    AmoCustomer,
    AmoCustomerCompany,
    AmoCustomerContact,
    AmoLead,
    AmoLeadContact,
    db,
)
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000

_AMBIGUOUS = object()


class _AbstractIdSequence:
    """Id для новых агрегатов берём из sequence БД пачками.

    Так новые abstract_customer получают id сразу (без flush'а ORM),
    а identity map сессии не растёт на весь прогон.
    """

    def __init__(self, chunk: int = 1000):
        self._chunk = chunk
        self._buffer: List[int] = []

    def next_id(self) -> int:
        if not self._buffer:
            rows = db.session.execute(
                text("SELECT nextval('abstract_customer_id_seq')"
                     " FROM generate_series(1, :n)"),
                {"n": self._chunk},
            ).scalars().all()
            self._buffer = rows[::-1]
        return self._buffer.pop()


class _DbMaps:
    """Предзагруженные индексы соответствия crm_id/логин -> abstract_customer.id."""

    def __init__(self):
        self.customer_crm: Dict[int, int] = {}    # amo_customer.crm_id -> id агрегата
        self.lead_crm: Dict[int, int] = {}        # amo_lead.crm_id -> id агрегата
        # логин -> id агрегата | _AMBIGUOUS
        self.by_customer_login: Dict[str, Any] = {}
        self.by_lead_login: Dict[str, Any] = {}
        self.merged_into: Dict[int, int] = {}     # проигравший -> победитель

    @classmethod
    def load(cls) -> "_DbMaps":
        maps = cls()
        rows = db.session.execute(
            select(AmoCustomer.crm_id, AmoCustomer.abstract_customer_id))
        for crm_id, abstract_id in rows:
            maps.customer_crm[crm_id] = abstract_id

        rows = db.session.execute(
            select(AmoLead.crm_id, AmoLead.abstract_customer_id, AmoLead.login))
        for crm_id, abstract_id, login in rows:
            maps.lead_crm[crm_id] = abstract_id
            maps._track(maps.by_lead_login, login, abstract_id)

        rows = db.session.execute(
            select(AmoCustomer.login, AmoCustomer.abstract_customer_id)
            .where(AmoCustomer.login.isnot(None)))
        for login, abstract_id in rows:
            maps._track(maps.by_customer_login, login, abstract_id)
        return maps

    @staticmethod
    def _track(mapping: Dict[str, Any], login: Optional[str], abstract_id: int) -> None:
        if not login:
            return
        current = mapping.get(login)
        if current is None:
            mapping[login] = abstract_id
        elif current is not _AMBIGUOUS and current != abstract_id:
            mapping[login] = _AMBIGUOUS

    def track_customer_login(self, login: str, abstract_id: int) -> None:
        self._track(self.by_customer_login, login, abstract_id)

    def track_lead_login(self, login: str, abstract_id: int) -> None:
        self._track(self.by_lead_login, login, abstract_id)

    def resolve(self, abstract_id: int) -> int:
        """Итоговый адресат после слияний (цепочки проигравших)."""
        seen = set()
        while abstract_id in self.merged_into:
            if abstract_id in seen:
                raise RuntimeError(f"Цикл слияний для {abstract_id}")
            seen.add(abstract_id)
            abstract_id = self.merged_into[abstract_id]
        return abstract_id

    def merge(self, loser: int, winner: int) -> int:
        """
        Две разные записи сливаются в найденную по сделке (winner).

        Перенос детей и удаление проигравшего откладываются
        до конца загрузки (_apply_merges).
        """
        loser = self.resolve(loser)
        winner = self.resolve(winner)
        if loser != winner:
            self.merged_into[loser] = winner
        return winner


class _SeenEntities:
    """
    Идентификаторы сущностей этой выгрузки.

    Дедупликация внутри прогона (защита от «ON CONFLICT ... cannot affect
    row a second time» в одном executemany) и основа чистки.
    """

    def __init__(self):
        self.customers: Set[int] = set()
        self.leads: Set[int] = set()
        self.contacts: Set[int] = set()
        self.companies: Set[int] = set()
        self.customer_contacts: Set[Tuple[int, int]] = set()
        self.customer_companies: Set[Tuple[int, int]] = set()
        self.lead_contacts: Set[Tuple[int, int]] = set()


class _BatchWriter:
    """Копит строки и пишет их пачками executemany, один commit."""

    def __init__(self):
        self._ids = _AbstractIdSequence()
        self._abstracts: List[Dict[str, Any]] = []
        self._customers: List[Dict[str, Any]] = []
        self._leads: List[Dict[str, Any]] = []
        self._contacts: List[Dict[str, Any]] = []
        self._companies: List[Dict[str, Any]] = []
        self._customer_contacts: List[Tuple[int, int]] = []
        self._customer_companies: List[Tuple[int, int]] = []
        self._lead_contacts: List[Tuple[int, int]] = []
        self._count = 0

    # наполнение

    def new_abstract_id(self) -> int:
        abstract_id = self._ids.next_id()
        self._abstracts.append({"id": abstract_id})
        self._count += 1
        return abstract_id

    def add_customer(self, abstract_id: int, customer: Customer,
                     seen: _SeenEntities) -> None:
        row = customer_row(customer)
        row["abstract_customer_id"] = abstract_id
        self._customers.append(row)
        seen.customers.add(customer.id)
        self._count += 1
        for contact in customer.contacts or []:
            self._add_contact(contact, seen)
            self._add_link(self._customer_contacts, seen.customer_contacts,
                           customer.id, contact)
        for company in customer.companies or []:
            self._add_company(company, seen)
            self._add_link(self._customer_companies, seen.customer_companies,
                           customer.id, company)

    def add_lead(self, abstract_id: int, lead: Lead, seen: _SeenEntities) -> None:
        row = lead_row(lead)
        row["abstract_customer_id"] = abstract_id
        self._leads.append(row)
        seen.leads.add(lead.id)
        self._count += 1
        for contact in lead.contacts or []:
            self._add_contact(contact, seen)
            self._add_link(self._lead_contacts, seen.lead_contacts,
                           lead.id, contact)

    def _add_contact(self, contact: Dict[str, Any], seen: _SeenEntities) -> None:
        crm_id = contact.get("id")
        if crm_id is None or crm_id in seen.contacts:
            return
        seen.contacts.add(crm_id)
        self._contacts.append(contact_row(contact))
        self._count += 1

    def _add_company(self, company: Dict[str, Any], seen: _SeenEntities) -> None:
        crm_id = company.get("id")
        if crm_id is None or crm_id in seen.companies:
            return
        seen.companies.add(crm_id)
        self._companies.append(company_row(company))
        self._count += 1

    @staticmethod
    def _add_link(pairs: List[Tuple[int, int]], seen_pairs: Set[Tuple[int, int]],
                  parent_crm_id: int, child: Dict[str, Any]) -> None:
        child_crm_id = child.get("id")
        if child_crm_id is None:
            return
        pair = (parent_crm_id, child_crm_id)
        if pair in seen_pairs:
            return
        seen_pairs.add(pair)
        pairs.append(pair)

    # запись

    def maybe_flush(self) -> None:
        if self._count >= BATCH_SIZE:
            self.flush()

    def flush(self) -> None:
        session = db.session
        try:
            if self._abstracts:
                session.execute(insert(AbstractCustomer), self._abstracts)
            # Порядок важен: сначала родители (агрегаты, контакты, компании),
            # потом ссылающиеся на них покупатели сделки и связи.
            for rows, model, update_cols in (
                (self._contacts, AmoContact, ("name", "api_value")),
                (self._companies, AmoCompany, ("name", "api_value")),
                (self._customers, AmoCustomer,
                 ("abstract_customer_id", "login", "name", "api_value")),
                (self._leads, AmoLead,
                 ("abstract_customer_id", "login", "name", "api_value")),
            ):
                if not rows:
                    continue
                stmt = pg_insert(model)
                set_ = {col: getattr(stmt.excluded, col) for col in update_cols}
                set_["updated_at"] = func.now()
                stmt = stmt.on_conflict_do_update(
                    index_elements=[model.crm_id], set_=set_)
                session.execute(stmt, rows)
            for pairs, model, first_col, second_col in (
                (self._customer_contacts, AmoCustomerContact,
                 "amo_customer_crm_id", "amo_contact_crm_id"),
                (self._customer_companies, AmoCustomerCompany,
                 "amo_customer_crm_id", "amo_company_crm_id"),
                (self._lead_contacts, AmoLeadContact,
                 "amo_lead_crm_id", "amo_contact_crm_id"),
            ):
                if not pairs:
                    continue
                stmt = pg_insert(model).on_conflict_do_nothing()
                session.execute(
                    stmt, [{first_col: a, second_col: b} for a, b in pairs])
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            self._abstracts.clear()
            self._customers.clear()
            self._leads.clear()
            self._contacts.clear()
            self._companies.clear()
            self._customer_contacts.clear()
            self._customer_companies.clear()
            self._lead_contacts.clear()
            self._count = 0


def collect_leads_customers(is_full=True):
    """Ночная синхронизация сущностей amoCRM в Postgres."""
    maps = _DbMaps.load()
    writer = _BatchWriter()
    seen = _SeenEntities()
    try:
        errors = _sync_customers(is_full=is_full, maps=maps, writer=writer,
                                 seen=seen)
        errors += _sync_leads(is_full=is_full, maps=maps, writer=writer,
                              seen=seen)
        writer.flush()
        _apply_merges(maps)
    except Exception:
        db.session.rollback()
        logger.exception("Сбор прерван: удаление не запускается")
        raise
    if is_full is False:
        return
    if errors:
        logger.warning(
            "Прогон неполный: %d ошибок обработки, чистка пропущена", errors)
        return
    try:
        _cleanup(seen)
    except Exception:
        db.session.rollback()
        logger.exception("Чистка: ошибка")
        raise


def _sync_customers(is_full: bool, maps: _DbMaps, writer: _BatchWriter,
                    seen: _SeenEntities) -> int:
    customers: Iterator[Customer] = amocrm_get(
        Customer, _with=f"{COMPANIES},{CONTACTS}")
    logger.info(f"Collected customers {is_full=}")
    errors = 0
    for customer in customers:
        if is_full is False and customer.status_id == 111349:
            continue
        customer_login = customer.get_field("Логин")
        if not customer_login:
            continue  # покупатель без логина пропускается
        try:
            abstract_id = maps.customer_crm.get(customer.id)
            if abstract_id is not None:
                abstract_id = maps.resolve(abstract_id)
            else:
                # почти всегда логины совпадают: ищем среди сохранённых сделок
                found = maps.by_lead_login.get(customer_login)
                if found is _AMBIGUOUS:
                    logger.warning(
                        "Login %s: больше одной записи по сделкам, "
                        "совпадение не используется", customer_login)
                elif found is not None:
                    abstract_id = maps.resolve(found)
            if abstract_id is None:
                abstract_id = writer.new_abstract_id()
            writer.add_customer(abstract_id, customer, seen)
            maps.customer_crm[customer.id] = abstract_id
            maps.track_customer_login(customer_login, abstract_id)
            writer.maybe_flush()
        except Exception:
            errors += 1
            logger.exception(f"Ошибка обработки покупателя {customer}")
    writer.flush()
    return errors


def _sync_leads(is_full: bool, maps: _DbMaps, writer: _BatchWriter,
                seen: _SeenEntities) -> int:
    leads: Iterator[Lead] = amocrm_get(Lead, _with=f"{CONTACTS}")
    logger.info(f"Collected leads {is_full=}")
    errors = 0
    for amo_lead in leads:
        if is_full is False and amo_lead.status_id == 143:
            continue
        try:
            _handle_lead(amo_lead, maps, writer, seen)
            writer.maybe_flush()
        except Exception:
            errors += 1
            logger.exception(f"Ошибка обработки сделки {amo_lead}")
    writer.flush()
    return errors


def _handle_lead(lead: Lead, maps: _DbMaps, writer: _BatchWriter,
                 seen: _SeenEntities) -> None:
    """
    Матчинг сделки: по id в CRM, иначе по логину среди покупателей.

    Нашлись две разные записи — сливаются в найденную по сделке.
    """
    lead_login = lead.get_field("Логин")

    by_lead_id = maps.lead_crm.get(lead.id)
    abstract_id = maps.resolve(by_lead_id) if by_lead_id is not None else None

    login_match: Optional[int] = None
    if lead_login:
        found = maps.by_customer_login.get(lead_login)
        if found is _AMBIGUOUS:
            logger.warning(
                "Login %s: больше одной записи по покупателям, "
                "совпадение не используется", lead_login)
        elif found is not None:
            login_match = maps.resolve(found)

    if abstract_id is not None and login_match is not None:
        if abstract_id != login_match:
            maps.merge(loser=login_match, winner=abstract_id)
    elif abstract_id is None:
        abstract_id = login_match
    if abstract_id is None:
        abstract_id = writer.new_abstract_id()  # не нашлось ничего — новая запись

    writer.add_lead(abstract_id, lead, seen)
    maps.lead_crm[lead.id] = abstract_id
    if lead_login:
        maps.track_lead_login(lead_login, abstract_id)


def _apply_merges(maps: _DbMaps) -> None:
    """
    Физическое слияние: дети переезжают

    к победителю, проигравшие удаляются.
    """
    if not maps.merged_into:
        return
    session = db.session
    logger.info("Слияние дублей: %d", len(maps.merged_into))
    final = {loser: maps.resolve(loser) for loser in maps.merged_into}
    for loser, winner in final.items():
        session.execute(
            update(AmoCustomer)
            .where(AmoCustomer.abstract_customer_id == loser)
            .values(abstract_customer_id=winner, updated_at=func.now()))
        session.execute(
            update(AmoLead)
            .where(AmoLead.abstract_customer_id == loser)
            .values(abstract_customer_id=winner, updated_at=func.now()))
    session.execute(
        delete(AbstractCustomer).where(AbstractCustomer.id.in_(list(final))))
    session.commit()


def _cleanup(seen: _SeenEntities) -> None:
    """
    Чистка после успешного полного прохода.

    Удаляем сущности, которых не было в выгрузке (в том числе с «удалить»
    в названии), устаревшие связи и опустевшие агрегаты — сеточно, без
    перечитки таблиц в Python.
    """
    session = db.session
    logger.info("Чистка покупателей общая")
    _delete_stale_links(seen)
    logger.info("Чистка покупателей на уровне сущностей")
    for model, ids in (
        (AmoLead, seen.leads),
        (AmoCustomer, seen.customers),
        (AmoContact, seen.contacts),
        (AmoCompany, seen.companies),
    ):
        session.execute(
            delete(model).where(
                text("NOT (crm_id = ANY(CAST(:ids AS bigint[])))"
                     " OR position('удалить' in lower(coalesce(name, ''))) > 0")),
            {"ids": list(ids)},
        )
    logger.info("Чистка опустевших агрегатов")
    session.execute(text("""
        DELETE FROM abstract_customer ac
        WHERE NOT EXISTS (SELECT 1 FROM amo_customer c
                          WHERE c.abstract_customer_id = ac.id)
          AND NOT EXISTS (SELECT 1 FROM amo_lead l
                          WHERE l.abstract_customer_id = ac.id)
    """))
    session.commit()


def _delete_stale_links(seen: _SeenEntities) -> None:
    """Удаляем связи, которых не было в выгрузке (пары — через temp-таблицы)."""
    session = db.session
    chunk = BATCH_SIZE * 10
    for model, first_col, second_col, pairs in (
        (AmoCustomerContact, "amo_customer_crm_id", "amo_contact_crm_id",
         seen.customer_contacts),
        (AmoCustomerCompany, "amo_customer_crm_id", "amo_company_crm_id",
         seen.customer_companies),
        (AmoLeadContact, "amo_lead_crm_id", "amo_contact_crm_id",
         seen.lead_contacts),
    ):
        table = model.__tablename__
        if not pairs:
            session.execute(delete(model))  # выгрузка пуста — все связи устарели
            continue
        temp = f"{table}_seen"
        session.execute(text(
            f"CREATE TEMP TABLE {temp} (a bigint NOT NULL, b bigint NOT NULL,"
            " PRIMARY KEY (a, b)) ON COMMIT DROP"))
        rows = [{"a": a, "b": b} for a, b in pairs]
        for start in range(0, len(rows), chunk):
            session.execute(text(f"INSERT INTO {temp} (a, b) VALUES (:a, :b)"),
                            rows[start:start + chunk])
        session.execute(text(
            f"DELETE FROM {table} t WHERE NOT EXISTS"
            f" (SELECT 1 FROM {temp} s"
            f" WHERE s.a = t.{first_col} AND s.b = t.{second_col})"))
