from fastapi import FastAPI
from ticketmind.api.routes.tickets import router as tickets_router
from ticketmind.api.routes.runs import router as runs_router
from ticketmind.api.routes.sources import router as sources_router
from ticketmind.core.config import ProcessingSettings
from ticketmind.core.errors import install_error_handlers
from ticketmind.db.session import SesstionLocal

def create_app(*, session_factory=SesstionLocal, runner=None, auth_settings=None, processing_settings=None):
    application = FastAPI(title="TicketMind")
    application.state.session_factory = session_factory
    application.state.runner = runner
    application.state.auth_settings = auth_settings
    application.state.processing_settings = processing_settings or ProcessingSettings()
    install_error_handlers(application)
    application.include_router(tickets_router)
    application.include_router(runs_router)
    application.include_router(sources_router)

    @application.get("/health", tags=["system"])
    async def health_check():
        return {"status": "ok"}

    return application


app = create_app()
