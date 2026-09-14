from fastapi import FastAPI
from contextlib import asynccontextmanager
from ticketmind.agent.review import ReviewWorkflow
from ticketmind.db.checkpoints import checkpoint_resources
from ticketmind.tickets.reviews import recover_interrupted_runs
from ticketmind.api.routes.tickets import router as tickets_router
from ticketmind.api.routes.runs import router as runs_router
from ticketmind.api.routes.sources import router as sources_router
from ticketmind.core.config import ProcessingSettings
from ticketmind.core.errors import install_error_handlers
from ticketmind.db.session import SesstionLocal

def create_app(*, session_factory=SesstionLocal, runner=None, auth_settings=None, processing_settings=None):
    @asynccontextmanager
    async def lifespan(application):
        with session_factory() as session:
            engine = session.get_bind()
        with checkpoint_resources(engine) as saver:
            application.state.workflow = ReviewWorkflow(saver)
            recover_interrupted_runs(session_factory)
            yield
        application.state.workflow = None

    application = FastAPI(title="TicketMind", lifespan=lifespan)
    application.state.workflow = None
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
