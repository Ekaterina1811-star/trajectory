"""Маппинг сущностей amoCRM в строки таблиц и поиск агрегата по логину."""

import logging
from typing import Any, Dict, List, Optional

from amocrm import Customer, Lead
from models import AbstractCustomer, AmoCustomer, AmoLead, db
from sqlalchemy import select

logger = logging.getLogger(__name__)


def customer_row(customer: Customer) -> Dict[str, Any]:
    """
    Строка таблицы amo_customer:

    ключевые поля — в колонках, сырой ответ API — в api_value.
    """
    return {
        "crm_id": customer.id,
        "login": customer.get_field("Логин"),
        "name": customer.name,
        "api_value": customer.as_dict(),
    }


def lead_row(lead: Lead) -> Dict[str, Any]:
    """Строка таблицы amo_lead."""
    return {
        "crm_id": lead.id,
        "login": lead.get_field("Логин"),
        "name": lead.name,
        "api_value": lead.as_dict(),
    }


def contact_row(contact: Dict[str, Any]) -> Dict[str, Any]:
    """
    Строка таблицы amo_contact;

    на входе — элемент _embedded из ответа API.
    """
    return {
        "crm_id": contact.get("id"),
        "name": contact.get("name"),
        "api_value": contact,
    }


def company_row(company: Dict[str, Any]) -> Dict[str, Any]:
    """
    Строка таблицы amo_company;

    на входе — элемент _embedded из ответа API.
    """
    return {
        "crm_id": company.get("id"),
        "name": company.get("name"),
        "api_value": company,
    }


def _abstract_ids_by_login(entity_model, login: str) -> List[int]:
    stmt = select(entity_model.abstract_customer_id).distinct().where(
        entity_model.login == login)
    return db.session.scalars(stmt).all()


def get_abccustomer_by_lead_login(
        lead_login: str
) -> Optional[AbstractCustomer]:
    """
    Поиск агрегата по логину среди сохранённых сделок.

    Если по логину нашлось больше одной записи — совпадение не используется.
    """
    ids = _abstract_ids_by_login(AmoLead, lead_login)
    if len(ids) > 1:
        logger.warning(f"Login {lead_login} has more than one abccustomer")
        return None
    return db.session.get(AbstractCustomer, ids[0]) if ids else None


def get_abccustomer_by_amocustomer_login(
        customer_login: str
) -> Optional[AbstractCustomer]:
    """Поиск агрегата по логину среди сохранённых покупателей."""
    ids = _abstract_ids_by_login(AmoCustomer, customer_login)
    if len(ids) > 1:
        logger.warning(
            f"Customer {customer_login} has more than one abccustomer"
        )
        return None
    return db.session.get(AbstractCustomer, ids[0]) if ids else None
