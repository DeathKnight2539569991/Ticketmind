from fastapi import FastAPI
from ticketmind.api.routes.tickets import router as tickets_router

app = FastAPI()
app.include_router(tickets_router)

@app.get("/health",tags=["system"])
async def health_check()->dict[str,str]:
    """Report whether the API process is available."""
    return {"status":"ok"}