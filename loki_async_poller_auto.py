#!/usr/bin/python3
import aiohttp
import asyncio
import datetime
import logging
import os
import socket
import subprocess
import sys
import time
import yaml

PROGRAM_PATH = "/opt/loki/"
LOKI_CONFIG_FILE = f"{PROGRAM_PATH}LOKI_INSTALLATIONS.YAML"
LOKI_LOG_PATH = f"{PROGRAM_PATH}log"
LOKI_POLLER_LOG_FILE = f"{LOKI_LOG_PATH}/loki_logs.log"
LOKI_REQUEST_TIMESTAMP_PATH = f"{PROGRAM_PATH}tmp"
LOKI_REQUEST_TIMESTAMP_FILE = f"{LOKI_REQUEST_TIMESTAMP_PATH}/loki_last_timestamp.txt"
SERVICE_PATH = "/etc/systemd/system/loki_api.service"


if not os.path.exists(LOKI_POLLER_LOG_FILE):
    # Создание файла для логирования
    os.makedirs(LOKI_LOG_PATH, exist_ok=True)
    with open(f'{LOKI_POLLER_LOG_FILE}', 'w+') as file:
        file.write(f"INFO: Создан файл для логирования скрипта {os.path.basename(__file__)}")

logging.basicConfig(filename=LOKI_POLLER_LOG_FILE, level=logging.INFO, format="%(asctime)s - %(message)s")

try:
    with open(LOKI_CONFIG_FILE, 'r') as installations_info_file:
        # Парсинг YAML файла для получения информации о сервисах
        installations_info = yaml.safe_load(installations_info_file)
        LOKI_IP = installations_info.get("loki_ip", None)
        if not LOKI_IP:
            logging.error(f"ERROR: Отсутствует параметр 'loki_ip' в конфигурационном файле: {LOKI_CONFIG_FILE}.")
            sys.exit(1)

        LOG_SOURCES = installations_info.get("log_sources", None)
        if not LOG_SOURCES:
            logging.error(f"ERROR: Отсутствует параметр 'log_sources' в конфигурационном файле: {LOKI_CONFIG_FILE}")
            sys.exit(1)
        containers = list(LOG_SOURCES.keys())

        for i_container in containers:
            services = LOG_SOURCES.get(i_container)
            for service_info in services:
                service = service_info.get('service_name', None)
                if not service:
                    logging.error(f"ERROR: Отсутствует параметр 'service_name' в конфигурационном файле: {LOKI_CONFIG_FILE}")
                    sys.exit(1)
                lst = service_info.get('log_source_type', None)
                if not lst:
                    logging.error(f"ERROR: Отсутствует параметр 'log_source_type' для сервиса {service} "
                          f"в конфигурационном файле: {LOKI_CONFIG_FILE}.\n"
                          f"Структура сервиса: log_sources->{i_container}->{service}")
                    sys.exit(1)
except:
    logging.error(f"ERROR: Ошибка синтаксиса конфигурационного файла: {LOKI_CONFIG_FILE}")
    sys.exit(1)

def loki_service_creator():
    """Создаёт сервис linux, который будет циклично вызывать скрипт после завершения его выполнения"""

    with open(SERVICE_PATH, "w") as loki_service_file:
        loki_service_file.write(
            "[Unit]\n"
            "Description=LOKI API SERVICE\n"
            "Wants=network-online.target\n"
            "After=network.target network-online.target\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart=/usr/bin/python3 {PROGRAM_PATH}{os.path.basename(__file__)}\n"
            "Restart=always\n"
            "RestartSec=0\n"
            "StartLimitInterval=0\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n\n"
        )

    subprocess.run(["sudo", "systemctl", "daemon-reload"])
    subprocess.run(["sudo", "systemctl", "start", "loki_api.service"])
    subprocess.run(["sudo", "systemctl", "enable", "loki_api.service"])

    return


def get_time_range():
    """
    Создаёт временные метки для API запросов в Loki

    Returns:
        start_ns_timestamp, end_ns_timestamp (tuple): Кортеж временных меток в наносекундах типа str
    """

    if not os.path.exists(LOKI_REQUEST_TIMESTAMP_PATH):
        os.makedirs(LOKI_REQUEST_TIMESTAMP_PATH, exist_ok=True)
        with open(LOKI_REQUEST_TIMESTAMP_FILE, "w") as timestamp_file:
            timestamp_file.write(str(int(time.time_ns())))

    with open(LOKI_REQUEST_TIMESTAMP_FILE, "r") as timestamp_file:
        start_ns_timestamp = timestamp_file.read().strip()

    end_ns_timestamp = str(int(time.time_ns()))
    with open(LOKI_REQUEST_TIMESTAMP_FILE, "w") as timestamp_file:
        timestamp_file.write(end_ns_timestamp)

    return start_ns_timestamp, end_ns_timestamp


async def fetch_loki_data(url: str, params: dict, container_identifier_tags: list):
    """
    Делает асинхронные запросы в Loki, согласно переданным URL и параметру запроса

    Parameters:
        url (str): URL для запроса в API
        params (dict): Параметры запроса в API

    Returns:
        response_data (dict): Ответ от сервера в виде json структуры
        container_identifier_tags (list): Информация о контейнере к которому делается запрос

    """
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params) as response:
                if not response.status == 200:
                    logging.error(f"ERROR: Ошибка запроса к Loki: {response.status}")
                    return None
                response_data = await response.json()
                return response_data, container_identifier_tags

        except Exception as exception:
            logging.error(f"ERROR: Ошибка при запросе к Loki: {exception}")
            return None

    # TODO сделать отправку событий сразу после получения ответа


async def send_syslog_message(events: list, port: int):
    """
        Отправляет события на UDP сокет localhost-а

        Parameters:
            events (list): Список событий в формате RFC3164
            port (int): Номер порта для отправки событий
    """

    syslog_server = ("127.0.0.1", port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for i_event in events:
            sock.sendto(i_event.encode("utf-8"), syslog_server)
    finally:
        sock.close()


async def process_json_and_collect_events(json_response: dict, installation_metadata: list):
    """
        Создаёт список событий инсталляции в формате RFC 3164

        Parameters:
            json_response (dict): JSON структура ответа от сервера Loki
            installation_metadata (list): Список с идентификатором контейнера и его названием

        Returns:
            events (list): Список событий в формтае RFC 3164 от определённой инсталляции
            port (int): Номер порта для отправки событий
    """

    events = []

    if not json_response or "data" not in json_response or "result" not in json_response["data"]:
        logging.error("ERROR: Некорректная структура ответа от Loki")
        return events

    container_identifier, service_name = installation_metadata
    hostname = log_sources[container_identifier][service_name].get("hostname", "unsetted_hostname")
    syslog_programname = log_sources[container_identifier][service_name].get("syslog_service_name")
    port = log_sources[container_identifier][service_name].get("port", 514)

    streams_list = json_response["data"]["result"]

    for i_stream in streams_list:
        for i_value in i_stream["values"]:
            timestamp, log_msg = i_value

            if service_name == "gitlab" and '{\"' in log_msg:
                log_msg.replace(r'\"', '"')
            if not log_msg:
                continue

            timestamp = datetime.datetime.fromtimestamp(int(timestamp) / 1e9).strftime("%Y-%m-%d %H:%M:%S")
            message = f"{timestamp} {hostname} {syslog_programname}: {log_msg}"
            events.append(message)

    return events, port


async def process_loki_instances(loki_urls_with_params: list):
    """
    Создаёт таски для асинхронных запросов в API Loki, после чего отправляет полученную
    json структуру на парсинг и вызывает функцию для отправки событий на порт

    Parameters:
        loki_urls_with_params (list): Спискок кортежей из URLа и параметров для запроса в API
    """

    tasks = []
    for url, params, container_identifier_tags in loki_urls_with_params:
        tasks.append(fetch_loki_data(url, params, container_identifier_tags))

    responses_list = await asyncio.gather(*tasks)

    for i_response_metadata in responses_list:
        events, port = await process_json_and_collect_events(i_response_metadata[0], i_response_metadata[1])
        await send_syslog_message(events, port)


async def main():
    start_ns_timestamp, end_ns_timestamp = get_time_range()

    loki_urls_with_params = [
        (
            f"http://{LOKI_IP}:3100/loki/api/v1/query_range",
            {
                "query": f'{{{installation_container}="{service_info.get("service_name")}"}}',
                "start": start_ns_timestamp,
                "end": end_ns_timestamp,
                "limit": 5000
            },
            [f"{installation_container}", f"{service_info.get('service_name')}"]
        )
        if not service_info.get("regexp", False)
        else
        (
            f"http://{LOKI_IP}:3100/loki/api/v1/query_range",
            {
                "query": f'{{{installation_container}=~"{service_info.get("service_name")}"}}',
                "start": start_ns_timestamp,
                "end": end_ns_timestamp,
                "limit": 5000
            },
            [f"{installation_container}", f"{service_info.get('service_name')}"]
        )
        for installation_container in containers
        for service_info in LOG_SOURCES[installation_container]
    ]

    await process_loki_instances(loki_urls_with_params)


if __name__ == "__main__":
    if not os.path.exists(SERVICE_PATH) or not os.path.getsize(SERVICE_PATH) > 0:
        # Проверка на наличие сервиса
        loki_service_creator()
        logging.info("INFO: Сервис loki_api успешно создан.")

    asyncio.run(main())

    if os.path.exists(LOKI_POLLER_LOG_FILE) and os.path.getsize(LOKI_POLLER_LOG_FILE) > 5 * 1024 * 1024:
        open(LOKI_POLLER_LOG_FILE, "w").close()
        logging.info("INFO: Файл логов очищен, так как его размер превышал 5 МБ.")

    logging.info("INFO: Скрипт успешно завершил свою работу.")
    sys.exit(0)
