from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Sequence,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

db = SQLAlchemy(metadata=MetaData(naming_convention=naming_convention))


class AbstractCustomer(db.Model):
    """Агрегат всех сущностей amoCRM одного реального покупателя.

    Раньше сущности лежали JSONB-массивами прямо в этой таблице, из-за чего ночной
    прогон искал записи через `@>` и обновлял их через `jsonb_set` по одной.
    Теперь сущности amoCRM вынесены в отдельные таблицы и ссылаются сюда внешним
    ключом (каскадное удаление), а сырой ответ API каждой сущности хранится
    в колонке api_value — формат JSON сохранён.
    """

    __tablename__ = "abstract_customer"

    id = Column(BigInteger, Sequence("abstract_customer_id_seq"), primary_key=True)
    send_methods = Column(JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class AmoEntityMixin:
    """Общие колонки таблиц сущностей amoCRM."""

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    crm_id = Column(BigInteger, nullable=False, unique=True)
    name = Column(Text, nullable=True)
    api_value = Column(JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class AmoCustomer(AmoEntityMixin, db.Model):
    """Покупатель amoCRM (customers).

    Логин и название вынесены в индексированные колонки: ночной прогон ищет по id CRM
    и логину, другие части системы — по точному логину и названию.
    """

    __tablename__ = "amo_customer"
    __table_args__ = (Index("ix_amo_customer_name", "name"),)

    abstract_customer_id = Column(
        BigInteger,
        ForeignKey("abstract_customer.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    login = Column(Text, nullable=True, index=True)


class AmoLead(AmoEntityMixin, db.Model):
    """Сделка amoCRM (leads). Связь сделка-покупатель — через abstract_customer_id."""

    __tablename__ = "amo_lead"
    __table_args__ = (Index("ix_amo_lead_name", "name"),)

    abstract_customer_id = Column(
        BigInteger,
        ForeignKey("abstract_customer.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    login = Column(Text, nullable=True, index=True)


class AmoContact(AmoEntityMixin, db.Model):
    """Контакт amoCRM."""

    __tablename__ = "amo_contact"


class AmoCompany(AmoEntityMixin, db.Model):
    """Компания amoCRM."""

    __tablename__ = "amo_company"


class AmoCustomerContact(db.Model):
    """Связь покупатель — контакт."""

    __tablename__ = "amo_customer_contact"

    amo_customer_crm_id = Column(
        BigInteger, ForeignKey("amo_customer.crm_id", ondelete="CASCADE"), primary_key=True)
    amo_contact_crm_id = Column(
        BigInteger, ForeignKey("amo_contact.crm_id", ondelete="CASCADE"), primary_key=True)


class AmoCustomerCompany(db.Model):
    """Связь покупатель — компания."""

    __tablename__ = "amo_customer_company"

    amo_customer_crm_id = Column(
        BigInteger, ForeignKey("amo_customer.crm_id", ondelete="CASCADE"), primary_key=True)
    amo_company_crm_id = Column(
        BigInteger, ForeignKey("amo_company.crm_id", ondelete="CASCADE"), primary_key=True)


class AmoLeadContact(db.Model):
    """Связь сделка — контакт."""

    __tablename__ = "amo_lead_contact"

    amo_lead_crm_id = Column(
        BigInteger, ForeignKey("amo_lead.crm_id", ondelete="CASCADE"), primary_key=True)
    amo_contact_crm_id = Column(
        BigInteger, ForeignKey("amo_contact.crm_id", ondelete="CASCADE"), primary_key=True)
