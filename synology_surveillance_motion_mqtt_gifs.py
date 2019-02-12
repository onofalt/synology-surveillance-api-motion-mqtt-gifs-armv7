#!/usr/bin/env python3

import json
import requests
import logging
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.DEBUG,
                    format='[%(asctime)s] [%(levelname)s] (%(threadName)-10s) %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

# SYNO.API urls
synoApiLoginUrl = "{}/webapi/auth.cgi?api=SYNO.API.Auth&method=Login&version=2" \
                  "&session=SurveillanceStation&format=sid&account={}&passwd={}"
synoApiCamerasInfoUrl = "{}/webapi/entry.cgi?api=SYNO.SurveillanceStation.Camera&method=List&version=1&_sid={}"
synoApiEventQueryUrl = "{}/webapi/entry.cgi?api=SYNO.SurveillanceStation.Event&method=List" \
                       "&version=4&locked=0&reason=2&limit=1&cameraIds={}&_sid={}"
synoApiEventDownloadUrl = "{}/webapi/entry.cgi?api=SYNO.SurveillanceStation.Event" \
                          "&method=Download&version=4&analyevent=false&mountId=0" \
                          "&eventId={}&_sid={}"


def parse_config(config_path):
    with open(config_path, 'r') as config_file:
        config_data = json.load(config_file)
    return config_data


def syno_login(base_url, user, password):
    login_response = requests.get(synoApiLoginUrl.format(base_url, user, password), verify=False)
    logging.info('login_response status_code %s', login_response.status_code)

    if login_response.ok:
        login_data = json.loads(login_response.content.decode('utf-8'))
        if login_data["success"]:
            logging.info('login_response got sinotoken %s', login_data["data"]["sid"])
            return login_data["data"]["sid"]
        else:
            return ""

    else:
        login_response.raise_for_status()


def syno_info(base_url, sid):
    info_response = requests.get(synoApiCamerasInfoUrl.format(base_url, sid), verify=False)
    logging.info('info_response status_code %s', info_response.status_code)

    if info_response.ok:
        info_data = json.loads(info_response.content.decode('utf-8'))
        return info_data

    else:
        info_response.raise_for_status()


def syno_last_event(base_url, camera_id, sid):
    event_response = requests.get(synoApiEventQueryUrl.format(base_url, camera_id, sid),
                                  verify=False)
    logging.info('event_response status_code %s', event_response.status_code)

    if event_response.ok:
        event_data = json.loads(event_response.content.decode('utf-8'))
        if event_data["data"]["events"][0]["cameraId"] == camera_id:
            logging.info('found event for camera %s', event_data["data"]["events"][0]["camera_name"])
            return event_data["data"]["events"][0]["eventId"]
        else:
            return -1

    else:
        event_response.raise_for_status()


def syno_download_video(download_dir, base_url, event_id, sid):
    outfile_gif = '{}/{}.mp4'.format(download_dir, event_id)

    with open(outfile_gif, "wb") as f:
        logging.info('Downloading video for event id %i to %s .....', event_id, outfile_gif)
        download_response = requests.get(synoApiEventDownloadUrl.format(base_url, event_id, sid),
                                         verify=False, stream=True)
        logging.info('download_response status_code %s', download_response.status_code)

        if download_response.ok:
            total_length = download_response.headers.get('content-length')

            if total_length is None:  # no content length header
                f.write(download_response.content)
            else:
                dl = 0
                total_length = int(total_length)
                for data in download_response.iter_content(chunk_size=4096):
                    dl += len(data)
                    f.write(data)
                    done = int(50 * dl / total_length)
                    sys.stdout.write("\r[%s%s]" % ('=' * done, ' ' * (50 - done)))
                    sys.stdout.flush()
            logging.info('Downloading video for event id %i to %s .....DONE', event_id, outfile_gif)
            return outfile_gif

        else:
            download_response.raise_for_status()


def convert_video_gif(scale, skip_first_n_secs, max_length_secs, input_video, output_gif):
    logging.info('convert_video_gif scale %i skip_first_n_secs %i max_length_secs %i input_video %s output_gif %s',
                 scale, skip_first_n_secs, max_length_secs, input_video, output_gif)

    retcode = subprocess.call([
        "ffmpeg", "-stats", "-i", input_video, "-vf",
        "fps=15,scale={}:-1:flags=lanczos".format(scale),
        "-ss", "00:00:" + "{}".format(skip_first_n_secs).zfill(2), "-t", "{}".format(max_length_secs), "-y",
        str(output_gif)
    ])
    os.remove(input_video)
    return retcode


class CameraMotionEventHandler:
    def __init__(self, processed_events, base_url, camera, config, sid):
        self.base_url = base_url
        self.camera = camera
        self.config = config
        self.sid = sid
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.username_pw_set(username=self.config["mqtt_user"], password=self.config["mqtt_pwd"])
        # Keep a FIFO of files processed so we can guard against duplicate
        # events
        self.processed_events = processed_events

    def publish_event(self, event):
        event_file = Path(event.mp4_path)
        gif = self.convert_gif(event_file)
        if gif:
            self.publish_mqtt_message(gif, self.camera.topic_name)

    def publish_mqtt_message(self, gif):
        logging.info('publish_mqtt_message gif mqtt_server %s  mqtt_port %i mqtt_base_topic %s topic_name %s',
                     self.config["mqtt_server"], self.config["mqtt_port"], self.config["mqtt_base_topic"],
                     self.camera["topic_name"])

        self.mqtt_client.connect(self.config["mqtt_server"],
                                 self.config["mqtt_port"])
        retcode = self.mqtt_client.publish(
            self.config["mqtt_base_topic"] + "/" + self.camera["topic_name"], gif)
        return retcode

    def poll_event(self):
        logging.info('Start getting last camera event for camera %s %s', self.camera["id"], self.camera["topic_name"])
        event_id = syno_last_event(self.base_url, self.camera["id"], self.sid)
        if event_id > -1:
            if event_id in self.processed_events:
                logging.info('Event %s already processed', event_id)
                return None, None

            logging.info('Start downloading event video for event_id %s', event_id)
            mp4_file = syno_download_video(self.config["ffmpeg_working_folder"], self.base_url, event_id, self.sid)
            outfile_gif = '{}/{}.gif'.format(self.config["ffmpeg_working_folder"], event_id)
            convert_retcode = convert_video_gif(self.camera["scale"],
                                                self.camera["skip_first_n_secs"],
                                                self.camera["max_length_secs"],
                                                mp4_file, outfile_gif)
            if convert_retcode == 0:
                public_retcode = self.publish_mqtt_message('{}.gif'.format(event_id))
                if public_retcode:
                    self.processed_events.append(event_id)
                    logging.info('Done processing event_id %i', event_id)
                else:
                    logging.error('Invalid return code from mqtt publish for event id %i camera topic %s', event_id,
                                  self.camera["topic_name"])
            else:
                logging.error('Invalid return code from ffmpeg subprocess call for event id %i', event_id)
        else:
            logging.info('No event found for camera %s %s', self.camera["id"], self.camera["topic_name"])


def main():
    _, config_filename = sys.argv
    logging.info('Starting')
    logging.info('Parsing %s', config_filename)
    config = parse_config(config_filename)

    processed_events = deque(maxlen=100)
    logged_in = False
    try:
        while True:
            time.sleep(10)
            if not logged_in:
                sid = syno_login(config["synology_base_api_url"], config["synology_user"], config["synology_password"])
                if sid == "":
                    logging.error('Synology credentials not valid')
                    continue
                else:
                    logged_in = True
                    logging.info('Synology Auth ok %s', sid)
                    info_data = syno_info(config["synology_base_api_url"], sid)
                    for camera_info in info_data["data"]["cameras"]:
                        logging.info('Synology Info Camera Id %s Name %s IP %s', camera_info["id"], camera_info["name"],
                                     camera_info["host"])

            for camera in config["synology_cameras"]:
                logging.info('CameraMotionEventHandler  poll_event %s %s', camera["id"], camera["topic_name"])
                camera_handler = CameraMotionEventHandler(processed_events, config["synology_base_api_url"], camera,
                                                          config, sid)
                camera_handler.poll_event()

    except KeyboardInterrupt:
        logging.info('KeyboardInterrupt')

    logging.info('Ending')


if __name__ == "__main__":
    main()
