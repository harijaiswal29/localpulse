from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from localpulse.config import Settings
from localpulse.container import Container

PILOT_ANSWERS: dict[str, str] = {
    "shop_name": "Mane's Bakehouse",
    "address": "12 FC Road, Shivajinagar",
    "city": "Pune",
    "hours": "8am-9pm, closed Monday",
    "owner_whatsapp": "+919812345678",
    "phone": "+912025551234",
    "specialties": "Chocolate truffle cake ₹550, Modak box ₹300, Multigrain bread ₹90",
    "tone": "warm, homely",
    "languages": "English, Marathi",
    "festival_specials": "modaks for Ganesh Chaturthi, faral boxes for Diwali",
}


def make_test_settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        llm_gateway="mock",
        model_content="mock",
        model_router="mock",
        model_insights="mock",
        _env_file=None,
    )


@pytest.fixture
def container() -> Container:
    return Container(make_test_settings())


@pytest.fixture
def session(container: Container) -> Iterator[Session]:
    session = container.session()
    yield session
    session.close()


@pytest.fixture
def pilot_context(container: Container, session: Session):
    agent = container.onboarding_agent(session)
    ctx = agent.run("pilot-1", "bakery", PILOT_ANSWERS)
    container.ensure_client_tools(ctx)
    return ctx
