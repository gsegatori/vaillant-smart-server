#!/usr/bin/env python3
import os
import signal
import threading
import logging
from flask import Flask, jsonify
from datetime import datetime
import asyncio
from dotenv import load_dotenv
from client import vaillant_client

load_dotenv()

log = logging.getLogger('werkzeug')
log.setLevel(logging.WARNING)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s - %(levelname)s - %(message)s")

app = Flask(__name__)

loop = asyncio.new_event_loop()


def start_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()


threading.Thread(target=start_loop, daemon=True).start()


@app.route('/favicon.ico')
def favicon():
    return '', 204


@app.route('/boiler-consumption/<int:year>/<int:month>', methods=['GET'])
def api_boiler_consumption(year, month):
    future = asyncio.run_coroutine_threadsafe(vaillant_client.get_gas_consumption(month, year), loop)
    result = future.result()
    return jsonify(result)


@app.route('/boiler-consumption-current-month', methods=['GET'])
def api_boiler_consumption_current_month():
    now = datetime.now()
    future = asyncio.run_coroutine_threadsafe(vaillant_client.get_gas_consumption(now.month, now.year), loop)
    result = future.result()
    return jsonify(result)


@app.route('/zones', methods=['GET'])
def api_zones():
    future = asyncio.run_coroutine_threadsafe(vaillant_client.get_zones(), loop)
    result = future.result()
    return jsonify(result)


@app.route('/zone-info/<int:index>', methods=['GET'])
def api_zone_info(index):
    future = asyncio.run_coroutine_threadsafe(vaillant_client.get_zone_info(index), loop)
    result = future.result()
    return jsonify(result)


@app.route('/zone-update/<int:index>/<string:mode>', methods=['GET'])
def api_zone_update(index, mode):
    future = asyncio.run_coroutine_threadsafe(vaillant_client.update_zone_mode(index, mode), loop)
    result = future.result()
    return jsonify(result)


@app.route('/zone-set-temp/<int:index>/<float:temp>', methods=['GET'])
def api_zone_set_temp(index, temp):
    logging.debug(f"Setting temperature for zone {index} to {temp}°C")
    future = asyncio.run_coroutine_threadsafe(vaillant_client.update_zone_temperature(index, temp), loop)
    result = future.result()
    return jsonify(result)


@app.route('/get-water-pressure', methods=['GET'])
def api_get_water_pressure():
    future = asyncio.run_coroutine_threadsafe(vaillant_client.get_water_pressure(), loop)
    result = future.result()
    return jsonify(result)


@app.route('/get-system-info', methods=['GET'])
def api_get_system_info():
    future = asyncio.run_coroutine_threadsafe(vaillant_client.get_system_info(), loop)
    result = future.result()
    return jsonify(result)


def shutdown_server(signal, frame):
    logging.info("Shutdown signal received. Closing HTTP session...")

    if vaillant_client.api is not None:
        future = asyncio.run_coroutine_threadsafe(vaillant_client.api.aiohttp_session.close(), loop)
        future.result()
        logging.info("HTTP session closed.")

    loop.call_soon_threadsafe(loop.stop)
    logging.info("Event loop stopped. Flask server shut down.")


signal.signal(signal.SIGINT, shutdown_server)
signal.signal(signal.SIGTERM, shutdown_server)

if __name__ == '__main__':
    logging.info("Starting Flask server...")
    app.run(debug=False)
