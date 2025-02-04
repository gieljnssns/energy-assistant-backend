"""Main module for the energy assistant application."""
import asyncio
from contextlib import suppress
from datetime import date
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import threading
from typing import Final

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore
from colorlog import ColoredFormatter
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi_socketio import SocketManager  # type: ignore
import requests  # type: ignore
from sqlalchemy.ext.asyncio import AsyncSession
import yaml

from app.EmhassOptimizer import EmhassOptimizer
from app.api.device import OTHER_DEVICE
from app.api.main import router as api_router
from app.devices import StatesMultipleRepositories, StatesRepository
from app.devices.device import Device
from app.devices.evcc import EvccDevice
from app.devices.home import Home
from app.devices.homeassistant import Homeassistant
from app.devices.registry import DeviceTypeRegistry
from app.mqtt import MqttConnection
from app.settings import settings
from app.storage import Database, get_async_session, session_storage

from .constants import ROOT_LOGGER_NAME

FORMAT_DATE: Final = "%Y-%m-%d"
FORMAT_TIME: Final = "%H:%M:%S"
FORMAT_DATETIME: Final = f"{FORMAT_DATE} {FORMAT_TIME}"
MAX_LOG_FILESIZE = 1000000 * 10  # 10 MB


app = FastAPI(title="energy-assistant")
sio = SocketManager(app=app, cors_allowed_origins="*")

origins = [
    "http://localhost",
    "http://localhost:5000",
    "http://localhost:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")


async def async_handle_state_update(home: Home, state_repository: StatesRepository, optimizer: EmhassOptimizer | None, db: Database, session: AsyncSession) -> None:
    """Read the values from home assistant and process the update."""
    try:
        state_repository.read_states()
        await home.update_state(state_repository)
        if optimizer is not None:
            await home.update_power_consumption(state_repository, optimizer)
            optimizer.update_repository_states(home, state_repository)
        else:
            logging.error(
                "The variable optimizer is None in async_handle_state_update")
        state_repository.write_states()
        # print("Send refresh: " + get_home_message(home))
        if db:
            if home:
                await asyncio.gather(sio.emit('refresh', {'data': get_home_message(home)}), db.store_home_state(home, session))
            else:
                logging.error(
                    "The variable home is None in async_handle_state_update")
        else:
            logging.error(
                "The variable db is None in async_handle_state_update")
        if optimizer is not None and home is not None:
            optimizer.update_devices(home)
    except Exception:
        logging.exception("error during sending refresh")


async def background_task(home: Home, hass: Homeassistant, optimizer: EmhassOptimizer | None, mqtt: MqttConnection, db: Database) -> None:
    """Periodically read the values from home assistant and process the update."""
    last_update = date.today()
    async_session = await get_async_session()
    repositories: list[StatesRepository] = [hass]
    if mqtt is not None:
        repositories.append(mqtt)
    state_repository = StatesMultipleRepositories(repositories)
    while True:
        await sio.sleep(30)
        # delta_t = datetime.now().timestamp()
        # print("Start refresh from home assistant")
        today = date.today()
        try:
            if today != last_update:
                home.store_energy_snapshot()
            last_update = today
            async with async_session() as session:
                await async_handle_state_update(home, state_repository, optimizer, db, session)
        except Exception:
            logging.exception("error in the background task")
        # print(f"refresh from home assistant completed in {datetime.now().timestamp() - delta_t} s")


def get_self_sufficiency(consumed_solar_energy: float, consumed_energy: float) -> float:
    """Calulate the self sufficiency value."""
    return min(round(consumed_solar_energy / consumed_energy * 100) if consumed_energy > 0 else 0.0, 100)


def get_device_message(device: Device) -> dict:
    """Generate the update data message for a device."""
    if device.energy_snapshot is not None:
        consumed_energy_today = device.consumed_energy - \
            device.energy_snapshot.consumed_energy
        consumed_solar_energy_today = device.consumed_solar_energy - \
            device.energy_snapshot.consumed_solar_energy
    else:
        consumed_energy_today = 0
        consumed_solar_energy_today = 0
    result = {
        "device_id": str(device.id),
        "type": device.__class__.__name__,
        "power": device.power,
        "available": device.available,
        "today": {
            "consumed_solar_energy":  consumed_solar_energy_today,
            "consumed_energy": consumed_energy_today,
            "self_sufficiency": get_self_sufficiency(consumed_solar_energy_today, consumed_energy_today)
        },
    }
    result["attributes"] = device.attributes
    return result


def get_home_message(home: Home) -> str:
    """Generate the update data message for a home."""
    devices_messages = []
    if home.energy_snapshop is not None:
        consumed_energy_today = home.consumed_energy - \
            home.energy_snapshop.consumed_energy
        consumed_solar_energy_today = home.consumed_solar_energy - \
            home.energy_snapshop.consumed_solar_energy
    else:
        consumed_energy_today = 0
        consumed_solar_energy_today = 0

    other_power = home.home_consumption_power
    other_consumed_energy = consumed_energy_today
    other_consumed_solar_energy = consumed_solar_energy_today
    for device in home.devices:
        device_message = get_device_message(device)
        devices_messages.append(device_message)
        other_power = other_power - device.power
        other_consumed_energy = other_consumed_energy - \
            device_message["today"]["consumed_energy"]
        other_consumed_solar_energy = other_consumed_solar_energy - \
            device_message["today"]["consumed_solar_energy"]

    other_device = {
        "device_id": str(OTHER_DEVICE),
        "type": "other_device",
        "power": other_power,
        "available": True,
        "today": {
            "consumed_solar_energy":  other_consumed_solar_energy,
            "consumed_energy": other_consumed_energy,
            "self_sufficiency": get_self_sufficiency(other_consumed_solar_energy, other_consumed_energy)
        },
    }
    devices_messages.append(other_device)

    home_message = {
        "name": home.name,
        "power": {
            "solar_production": home.solar_production_power,
            "grid_supply": home.grid_imported_power,
            "solar_self_consumption": home.solar_self_consumption_power,
            "home_consumption": home.home_consumption_power,
            "self_sufficiency": round(home.self_sufficiency * 100)
        },
        "today": {
            "consumed_solar_energy":  consumed_solar_energy_today,
            "consumed_energy": consumed_energy_today,
            "self_sufficiency": get_self_sufficiency(consumed_solar_energy_today, consumed_energy_today)
        },
        "devices": devices_messages
    }
    return json.dumps(home_message)


@app.sio.event  # type: ignore
async def connect(sid, environ):
    """Handle the connect of a client via socket.io to the server."""
    logging.info(f"connect {sid}")
    await sio.emit('refresh', {'data': get_home_message(app.home)}, room=sid)


@app.sio.event  # type: ignore
def disconnect(sid):
    """Handle the disconnect of a client via socket.io to the server."""
    logging.info(f"Client disconnected {sid}")


def create_mqtt_connection(config: dict) -> MqttConnection | None:
    """Create an mqtt connection based on the config."""

    mqtt_config = config.get("mqtt")
    if mqtt_config is not None:
        mqtt_host = mqtt_config.get("host")
        mqtt_username = mqtt_config.get("username")
        mqtt_password = mqtt_config.get("password")
        mqtt_topic = mqtt_config.get("topic")
        mqtt_connection = MqttConnection(mqtt_host, mqtt_username, mqtt_password, mqtt_topic)
        mqtt_connection.connect()
        return mqtt_connection
    return None

def subscribe_mqtt_topics(mqtt_connection: MqttConnection, home: Home) -> None:
    """Subscribe the mqtt based devices on the mqtt connection."""

    for device in home.devices:
        if isinstance(device, EvccDevice):
            mqtt_connection.add_subscription_topic(device.evcc_mqtt_subscription_topic)


def create_hass_connection(config: dict) -> Homeassistant | None:
    """Create a connection to home assistant."""
    token : str | None = None
    url: str | None = None
    try:
        token = os.getenv('SUPERVISOR_TOKEN')
        if token is not None:
            logging.info(f"suvervisor token detected. len={len(token)}")
            url = "http://supervisor/core"

            headers = {
                "Authorization": f"Bearer {token}",
                "content-type": "application/json",
            }
            response = requests.get(
                f"{url}/api/states", headers=headers)
            logging.info(f"pinging homeassistant api succeeeded. Status code = {response.status_code}")
            if response.ok:
                logging.info(f"Using {url} to connect")
                hass = Homeassistant(url, token, False)
                return hass
    except Exception:
        logging.exception("Error while trying to connect to the homeassistant supervisor api")
        url = None
        token = None

    logging.info("Try to connect to home assistant based on the config file entries...")
    hass_config = config.get("homeassistant")
    if hass_config is not None:
        demo_mode = hass_config.get("demo_mode")
        url = hass_config.get("url")
        token = hass_config.get("token")
        if url is not None and token is not None:
            return Homeassistant(url, token, demo_mode)
    return None



def setup_logger(log_filename: str, level: str = "DEBUG") -> logging.Logger:
    """Initialize logger."""
    # define log formatter
    log_fmt = "%(asctime)s.%(msecs)03d %(levelname)s (%(threadName)s) [%(name)s] %(message)s"

    # base logging config for the root logger
    logging.basicConfig(level=logging.INFO)

    colorfmt = f"%(log_color)s{log_fmt}%(reset)s"
    logging.getLogger().handlers[0].setFormatter(
        ColoredFormatter(
            colorfmt,
            datefmt=FORMAT_DATETIME,
            reset=True,
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "red",
            },
        )
    )

    # Capture warnings.warn(...) and friends messages in logs.
    # The standard destination for them is stderr, which may end up unnoticed.
    # This way they're where other messages are, and can be filtered as usual.
    logging.captureWarnings(True)

    # setup file handler
    #log_filename = os.path.join(data_path, "energy_assistant.log")
    file_handler = RotatingFileHandler(log_filename, maxBytes=MAX_LOG_FILESIZE, backupCount=1)
    # rotate log at each start
    with suppress(OSError):
        file_handler.doRollover()
    file_handler.setFormatter(logging.Formatter(log_fmt, datefmt=FORMAT_DATETIME))
    # file_handler.setLevel(logging.INFO)

    logger = logging.getLogger()
    logger.addHandler(file_handler)

    # apply the configured global log level to the (root) music assistant logger
    logging.getLogger(ROOT_LOGGER_NAME).setLevel(level)

    # silence some noisy loggers
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("databases").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    sys.excepthook = lambda *args: logging.getLogger(None).exception(
        "Uncaught exception", exc_info=args  # type: ignore[arg-type]
    )
    threading.excepthook = lambda args: logging.getLogger(None).exception(
        "Uncaught thread exception",
        exc_info=(  # type: ignore[arg-type]
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
        ),
    )

    return logger


async def init_app() -> None:
    """Initialize the application."""
    app.home = None  # type: ignore
    app.hass = None  # type: ignore
    app.mqtt = None # type: ignore
    app.db = None  # type: ignore
    app.optimizer = None # type: ignore

    hass_options_file = "/data/options.json"
    if os.path.isfile(hass_options_file):
        with open(hass_options_file, "rb") as _file:
            hass_options = json.loads(_file.read())
    else:
        hass_options = {}

    log_level = hass_options.get("log_level", "info").upper()

    logger = setup_logger(settings.LOG_FILE, log_level)

    config_file = hass_options.get("config_file", settings.CONFIG_FILE)

    logger.info("Hello from Energy Assistant")

    for option in hass_options:
        logging.info(f"{option}={hass_options[option]}")


    async_session = await get_async_session()
    db = Database()
    app.db = db  # type: ignore

    device_type_registry = DeviceTypeRegistry()
    device_type_registry.load(settings.DEVICE_TYPE_REGISTRY)

    logger.info(f"Loading config file {config_file}")
    try:
        with open(config_file) as stream:
            logger.debug(f"Successfully opened config file {config_file}")
            try:
                config = yaml.safe_load(stream)
                logger.debug(f"config file {config_file} successfully loaded")
            except yaml.YAMLError:
                logger.exception("Failed to parse the config file")
            except Exception:
                logger.exception("Failed to parse the config file")
            else:
                hass = create_hass_connection(config)
                app.hass = hass  # type: ignore
                if hass is not None:
                    hass.read_states()

                mqtt_connection : MqttConnection | None = create_mqtt_connection(config)
                app.mqtt = mqtt_connection # type: ignore

                home_config = config.get("home")
                if home_config is not None and home_config.get("name") is not None:
                    home = Home(home_config, session_storage, device_type_registry)
                    app.home = home  # type: ignore
                    if mqtt_connection is not None:
                        subscribe_mqtt_topics(mqtt_connection, home)
                    app.optimizer = EmhassOptimizer(settings.DATA_FOLDER, config, hass) # type: ignore

                    async with async_session() as session:
                        await db.update_devices(home, session)

                        await db.restore_home_state(home, session)
                else:
                    logger.error(f"home not found in config file: {config}")
                logger.info("Initialization completed")
    except Exception :
        logger.exception("Initialization of the app failed")


async def optimize(optimizer: EmhassOptimizer) -> None:
    """Optimize the forcast."""
    try:
        # optimizer.perfect_forecast_optim("profit", False)
        optimizer.forecast_model_fit()
       # optimizer.forecast_model_predict()

        # optimizer.dayahead_forecast_optim()
    except Exception:
        logging.exception("Optimization failed")

def daily_optimize() -> None:
    """Optimze once a day."""
    try:
        optimizer = app.optimizer # type: ignore
        if optimizer is not None:
            logging.info("Start optimizer run")
            optimizer.dayahead_forecast_optim()
    except Exception :
        logging.exception("Daily optimization run failed")



@app.on_event("startup")
async def startup() -> None:
    """Statup call back to initialize the app and start the background task."""
    await init_app()
    sio.start_background_task(
        background_task, app.home, app.hass, app.optimizer, app.mqtt, app.db)  # type: ignore
    sio.start_background_task(optimize, app.optimizer) # type: ignore

    scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_optimize, trigger='cron', hour='3', minute='0') # time is UTC
    scheduler.start()



@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Stop call back to stop the app."""
    print("Shutdown app")



@app.get("/check", include_in_schema=False)
async def health() -> JSONResponse:
    """Test the web server with a ping."""
    return JSONResponse({"message": "It worked!!"})

# This needs to be the last mount
app.mount("/", StaticFiles(directory="client", html=True), name="frontend")


if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)
