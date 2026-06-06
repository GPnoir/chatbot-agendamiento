"""Punto de entrada del chatbot de agendamiento."""
import argparse
import asyncio

import uvicorn

import database as db
from config import HOST, PORT


async def run_polling():
    """Modo desarrollo: Telegram en polling + servidor para WhatsApp."""
    from channels.telegram_bot import create_telegram_app
    from channels.whatsapp_bot import router
    from fastapi import FastAPI

    db.init_db()
    print("✅ Base de datos inicializada")

    # FastAPI para WhatsApp
    app = FastAPI()
    app.include_router(router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # Uvicorn como tarea async
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="info")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    print(f"🌐 Servidor WhatsApp en http://{HOST}:{PORT}")

    # Telegram polling
    print("🤖 Telegram bot iniciado (polling)")
    telegram_app = create_telegram_app()
    async with telegram_app:
        await telegram_app.start()
        await telegram_app.updater.start_polling()
        # Mantener vivo hasta Ctrl+C
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            server.should_exit = True
            await server_task


def run_webhook():
    """Modo producción: ambos canales via webhook."""
    db.init_db()
    print(f"🚀 Servidor webhook en http://{HOST}:{PORT}")
    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chatbot de Agendamiento")
    parser.add_argument("--mode", choices=["polling", "webhook"], default="polling",
                        help="polling (dev) o webhook (prod)")
    args = parser.parse_args()

    if args.mode == "polling":
        try:
            asyncio.run(run_polling())
        except KeyboardInterrupt:
            print("\n👋 Bot detenido.")
    else:
        run_webhook()
