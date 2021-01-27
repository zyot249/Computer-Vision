
from hashlib import sha1
from shutil import rmtree
from stat import S_ISREG, ST_CTIME, ST_MODE
import json
import os
import time

from PIL import Image, ImageFile
from gevent.event import AsyncResult
from gevent.queue import Empty, Queue
from gevent.timeout import Timeout
import flask

import argparse
import datetime
import threading

import imutils
from cv2 import cv2
from flask import Flask, render_template, Response
from imutils.video import VideoStream

import numpy as np

from model.mask_detection.maskdetector import MaskDetector

# Define const
DATA_DIR = 'data'
CSS_FOLDER = 'static/css'
JS_FOLDER = 'static/js'
KEEP_ALIVE_DELAY = 25
MAX_IMAGE_SIZE = 800, 600
MAX_IMAGES = 10
MAX_DURATION = 300


outputFrame = None
lock = threading.Lock()

# initialize the video stream and allow the camera sensor to
# warmup
# vs = VideoStream(usePiCamera=1).start()
vs = VideoStream(src=0).start()
time.sleep(2.0)

APP = flask.Flask(__name__, static_folder=DATA_DIR)
BROADCAST_QUEUE = Queue()


try:  # Reset saved files on each start
    rmtree(DATA_DIR, True)
    os.mkdir(DATA_DIR)
except OSError:
    pass


def broadcast(message):
    """Notify all waiting waiting gthreads of message."""
    waiting = []
    try:
        while True:
            waiting.append(BROADCAST_QUEUE.get(block=False))
    except Empty:
        pass
    print('Broadcasting {} messages'.format(len(waiting)))
    for item in waiting:
        item.set(message)


def receive():
    """Generator that yields a message at least every KEEP_ALIVE_DELAY seconds.

    yields messages sent by `broadcast`.

    """
    now = time.time()
    end = now + MAX_DURATION
    tmp = None
    # Heroku doesn't notify when clients disconnect so we have to impose a
    # maximum connection duration.
    while now < end:
        if not tmp:
            tmp = AsyncResult()
            BROADCAST_QUEUE.put(tmp)
        try:
            yield tmp.get(timeout=KEEP_ALIVE_DELAY)
            tmp = None
        except Timeout:
            yield ''
        now = time.time()


def safe_addr(ip_addr):
    """Strip off the trailing two octets of the IP address."""
    return '.'.join(ip_addr.split('.')[:2] + ['xxx', 'xxx'])


def save_normalized_image(path, data):
    """Generate an RGB thumbnail of the provided image."""
    image_parser = ImageFile.Parser()
    try:
        image_parser.feed(data)
        image = image_parser.close()
    except Exception as e:
        print(str(e))
        return False
    image.thumbnail(MAX_IMAGE_SIZE, Image.ANTIALIAS)
    if image.mode != 'RGB':
        image = image.convert('RGB')
    image.save(path)
    return True

def save_normalized_image_with_detect(path, data):
    """Generate an RGB thumbnail of the provided image."""

    img = data
    npimg = np.fromstring(img, np.uint8)
    # convert numpy array to image
    img = cv2.imdecode(npimg, cv2.IMREAD_COLOR)
    cv2.imwrite("images/output.png", img)

    image3 = cv2.resize(img, (224, 224))
    image = np.expand_dims(image3, axis=0)

    imgarray = image


    md = MaskDetector()
    result = md.detect(imgarray)
    result.save(path)
    return True


def event_stream(client):
    """Yield messages as they come in."""
    force_disconnect = False
    try:
        for message in receive():
            yield 'data: {}\n\n'.format(message)
        print('{} force closing stream'.format(client))
        force_disconnect = True
    finally:
        if not force_disconnect:
            print('{} disconnected from stream'.format(client))


@APP.route('/post', methods=['POST'])
def post():
    """Handle image uploads."""
    sha1sum = sha1(flask.request.data).hexdigest()
    target = os.path.join(DATA_DIR, '{}.jpg'.format(sha1sum))
    message = json.dumps({'src': target,
                          'ip_addr': safe_addr(flask.request.access_route[0])})
    try:
        if save_normalized_image_with_detect(target, flask.request.data):
            broadcast(message)  # Notify subscribers of completion
    except Exception as exception:  # Output errors
        return '{}'.format(exception)
    return 'success'


@APP.route('/stream')
def stream():
    """Handle long-lived SSE streams."""
    return flask.Response(event_stream(flask.request.access_route[0]),
                          mimetype='text/event-stream')

def detect_mask():
    global vs, outputFrame, lock

    # initialize the motion detector and the total number of frames
    # read thus far
    md = MaskDetector()
    while True:
        # read the next frame from the video stream, resize it,
        # convert the frame to grayscale, and blur it
        frame = vs.read()
        frame = imutils.resize(frame, width=900)

        # grab the current timestamp and draw it on the frame
        timestamp = datetime.datetime.now()
        cv2.putText(frame, timestamp.strftime(
            "%A %d %B %Y %I:%M:%S%p"), (10, frame.shape[0] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 0, 255), 1)

        result = md.detect(frame)

        # acquire the lock, set the output frame, and release the
        # lock
        with lock:
            outputFrame = result

def generate():
    # grab global references to the output frame and lock variables
    global outputFrame, lock

    # loop over frames from the output stream
    while True:
        # wait until the lock is acquired
        with lock:
            # check if the output frame is available, otherwise skip
            # the iteration of the loop
            if outputFrame is None:
                continue

            # encode the frame in JPEG format
            (flag, encodedImage) = cv2.imencode(".jpg", outputFrame)

            # ensure the frame was successfully encoded
            if not flag:
                continue

        # yield the output frame in the byte format
        yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' +
               bytearray(encodedImage) + b'\r\n')

@APP.route("/video_feed")
def video_feed():
    # return the response generated along with the specific media
    # type (mime type)
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")

@APP.route("/css/<filename>")
def css_file(filename):
    return flask.send_from_directory(CSS_FOLDER, filename)

@APP.route("/js/<filename>")
def js_file(filename):
    return flask.send_from_directory(JS_FOLDER, filename)

@APP.route('/')
def home():
    """Provide the primary view along with its javascript."""
    # Code adapted from: http://stackoverflow.com/questions/168409/
    image_infos = []
    for filename in os.listdir(DATA_DIR):
        filepath = os.path.join(DATA_DIR, filename)
        file_stat = os.stat(filepath)
        if S_ISREG(file_stat[ST_MODE]):
            image_infos.append((file_stat[ST_CTIME], filepath))

    images = []
    for i, (_, path) in enumerate(sorted(image_infos, reverse=True)):
        if i >= MAX_IMAGES:
            os.unlink(path)
            continue
        images.append('<div><img alt="User uploaded image" src="{}" /></div>'
                      .format(path))
    return render_template('index.html') % (MAX_IMAGES, '\n'.join(images))  # noqa


if __name__ == '__main__':
    # construct the argument parser and parse command line arguments
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--ip", type=str, required=True,
                    help="ip address of the device")
    ap.add_argument("-o", "--port", type=int, required=True,
                    help="ephemeral port number of the server (1024 to 65535)")
    ap.add_argument("-f", "--frame-count", type=int, default=32,
                    help="# of frames used to construct the background model")
    args = vars(ap.parse_args())

    # start a thread that will perform motion detection
    t = threading.Thread(target=detect_mask)
    t.daemon = True
    t.start()

    APP.debug = True
    # start the flask app
    APP.run(host=args["ip"], port=args["port"], debug=True,
            threaded=True, use_reloader=False)

# release the video stream pointer
vs.stop()
