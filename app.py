#!/usr/bin/env python3
import os
import subprocess
import uuid
import logging
import json
import re
import gzip
import time
import io
import wave
import tempfile
import threading
import functools
import signal
import atexit
from collections import defaultdict

from flask import Flask, request, Response, jsonify, send_file, send_from_directory
from flask_cors import CORS
import requests
from thespian import ActorSystem

import utils
# import wake
from rhasspy import RhasspyActor
# from profiles import request_to_profile, Profile
# from stt import transcribe_wav, maybe_load_decoder
# from intent import best_intent
# from train import train
# from audio_recorder import PyAudioRecorder, ARecordAudioRecorder

# -----------------------------------------------------------------------------
# Flask Web App Setup
# -----------------------------------------------------------------------------

logging.basicConfig(level=logging.DEBUG)

app = Flask('rhasspy')
app.secret_key = str(uuid.uuid4())
CORS(app)

# -----------------------------------------------------------------------------
# Actor System Setup
# -----------------------------------------------------------------------------

system = ActorSystem('multiprocTCPBase')

# We really, *really* want shutdown to be called
@atexit.register
def shutdown(*args, **kwargs):
    system.shutdown()

signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

core = system.createActor(RhasspyActor)

# -----------------------------------------------------------------------------
# HTTP API
# -----------------------------------------------------------------------------

@app.route('/api/listen-for-wake', methods=['POST'])
def api_listen_for_wake():
    profile = request_to_profile(request, profiles_dirs)
    no_hass = request.args.get('nohass', 'false').lower() == 'true'
    device_index = int(request.args.get('device', -1))
    if device_index < 0:
        device_index = None  # default device

    return jsonify(listen_for_wake(profile, no_hass, device_index))

# -----------------------------------------------------------------------------

@app.route('/api/listen-for-command', methods=['POST'])
def api_listen_for_command():
    profile = request_to_profile(request, profiles_dirs)
    no_hass = request.args.get('nohass', 'false').lower() == 'true'

    # Listen until silence
    intent = listen_for_command(profile, no_hass)

    return jsonify(intent)

# -----------------------------------------------------------------------------

# Load default profile and (optionally) start listening for wake word
load_default_profile()

# -----------------------------------------------------------------------------

@app.route('/api/profiles')
def api_profiles():
    # ['en', 'fr', ...]
    profile_names = set()
    for profiles_dir in core.profiles_dirs:
        if not os.path.exists(profiles_dir):
            continue

        for path in os.listdir(profiles_dir):
            if os.path.isdir(os.path.join(profiles_dir, path)):
                profile_names.add(path)

    return jsonify({
        'default_profile': core.default_profile_name,
        'profiles': sorted(profile_names)
    })

# -----------------------------------------------------------------------------

@app.route('/api/profile', methods=['GET', 'POST'])
def api_profile():
    layers = request.args.get('layers', 'all')

    if request.method == 'POST':
        # Ensure that JSON is valid
        json.loads(request.data)

        if layers == 'default':
            # Write default settings
            for profiles_dir in profiles_dirs:
                profile_path = os.path.join(profiles_dir, 'defaults.json')
                if os.path.exists(profile_path):
                    with open(profile_path, 'wb') as profile_file:
                        profile_file.write(request.data)
        else:
            # Write profile settings
            profile = request_to_profile(request, profiles_dirs)
            profile_path = profile.write_path('profile.json')
            with open(profile_path, 'wb') as profile_file:
                profile_file.write(request.data)

        return 'Wrote %d byte(s) to %s' % (len(request.data), profile_path)

    profile = request_to_profile(request, profiles_dirs, layers)

    return jsonify(profile.json)

# -----------------------------------------------------------------------------

@app.route('/api/lookup', methods=['POST'])
def api_lookup():
    n = int(request.args.get('n', 5))
    assert n > 0, 'No pronunciations requested'

    voice = request.args.get('voice', None)

    profile = request_to_profile(request, profiles_dirs)
    ps_config = profile.speech_to_text['pocketsphinx']
    espeak_config = profile.text_to_speech['espeak']

    word = request.data.decode('utf-8').strip().lower()
    assert len(word) > 0, 'No word to look up'
    logging.debug('Getting pronunciations for %s' % word)

    # Load base and custom dictionaries
    base_dictionary_path = profile.read_path(ps_config['base_dictionary'])
    custom_path = profile.read_path(ps_config['custom_words'])

    word_dict = {}
    for word_dict_path in [base_dictionary_path, custom_path]:
        if os.path.exists(word_dict_path):
            with open(word_dict_path, 'r') as dictionary_file:
                utils.read_dict(dictionary_file, word_dict)

    result = utils.lookup_word(word, word_dict, profile, n=n)

    # Get phonemes from eSpeak
    espeak_command = ['espeak', '-q', '-x']

    if voice is None:
        if 'voice' in espeak_config:
            # Use profile voice
            voice = espeak_config['voice']
        elif 'language' in profile.json:
            # Use language default voice
            voice = profile.json['language']

    espeak_command.extend(['-v', voice, word])
    logging.debug(espeak_command)
    result['espeak_phonemes'] = subprocess.check_output(espeak_command).decode()

    return jsonify(result)

# -----------------------------------------------------------------------------

@app.route('/api/pronounce', methods=['POST'])
def api_pronounce():
    profile = request_to_profile(request, profiles_dirs)
    espeak_config = profile.text_to_speech['espeak']

    download = request.args.get('download', 'false').lower() == 'true'
    speed = int(request.args.get('speed', 80))
    voice = request.args.get('voice', None)

    pronounce_str = request.data.decode('utf-8').strip()
    assert len(pronounce_str) > 0, 'No string to pronounce'

    pronounce_type = request.args.get('type', 'phonemes')

    if pronounce_type == 'phonemes':
        # Load map from Sphinx to eSpeak phonemes
        map_path = profile.read_path(espeak_config['phoneme_map'])
        phoneme_map = utils.load_phoneme_map(map_path)

        # Convert from Sphinx to espeak phonemes
        espeak_str = "[['%s]]" % ''.join(phoneme_map.get(p, p)
                                         for p in pronounce_str.split())
    else:
        # Speak word directly
        espeak_str = pronounce_str

    # Generate WAV data
    espeak_command = ['espeak', '-s', str(speed)]

    if voice is None:
        if 'voice' in espeak_config:
            # Use profile voice
            voice = espeak_config['voice']
        elif 'language' in profile.json:
            # Use language default voice
            voice = profile.json['language']

    if voice is not None:
        espeak_command.extend(['-v', str(voice)])

    espeak_command.append(espeak_str)

    with tempfile.NamedTemporaryFile(suffix='.wav', mode='wb+') as wav_file:
        espeak_command.extend(['-w', wav_file.name])
        logging.debug(espeak_command)

        # Generate WAV data
        subprocess.check_call(espeak_command)
        wav_file.seek(0)

        if download:
            return Response(wav_file.read(), mimetype='audio/wav')
        else:
            subprocess.check_call(['aplay', '-t', 'wav', wav_file.name])
            return espeak_str

# -----------------------------------------------------------------------------

@app.route('/api/phonemes')
def api_phonemes():
    profile = request_to_profile(request, profiles_dirs)
    tts_config = profile.text_to_speech
    examples_path = profile.read_path(tts_config['phoneme_examples'])

    # phoneme -> { word, phonemes }
    logging.debug('Loading phoneme examples from %s' % examples_path)
    examples_dict = utils.load_phoneme_examples(examples_path)

    return jsonify(examples_dict)

# -----------------------------------------------------------------------------

@app.route('/api/sentences', methods=['GET', 'POST'])
def api_sentences():
    profile = request_to_profile(request, profiles_dirs)
    stt_config = profile.speech_to_text

    if request.method == 'POST':
        # Update sentences
        sentences_path = profile.write_path(stt_config['sentences_ini'])
        with open(sentences_path, 'wb') as sentences_file:
            sentences_file.write(request.data)
            return 'Wrote %s byte(s) to %s' % (len(request.data), sentences_path)

    # Return sentences
    sentences_path = profile.read_path(stt_config['sentences_ini'])
    if not os.path.exists(sentences_path):
        return ''  # no sentences yet

    # Return file contents
    return send_file(open(sentences_path, 'rb'),
                     mimetype='text/plain')

# -----------------------------------------------------------------------------

@app.route('/api/custom-words', methods=['GET', 'POST'])
def api_custom_words():
    profile = request_to_profile(request, profiles_dirs)
    stt_config = profile.speech_to_text
    ps_config = stt_config['pocketsphinx']
    custom_words_path = profile.write_path(ps_config['custom_words'])

    if request.method == 'POST':
        # Update custom words
        with open(custom_words_path, 'wb') as custom_words_file:
            custom_words_file.write(request.data)
            return 'Wrote %s byte(s) to %s' % (len(request.data), custom_words_path)

    # Return custom_words
    if not os.path.exists(custom_words_path):
        return ''  # no custom_words yet

    # Return file contents
    return send_file(open(custom_words_path, 'rb'),
                     mimetype='text/plain')

# -----------------------------------------------------------------------------

@app.route('/api/train', methods=['POST'])
def api_train():
    profile = request_to_profile(request, profiles_dirs)

    start_time = time.time()
    logging.info('Starting training')
    train(profile)
    end_time = time.time()

    reload(profile)

    return 'Training completed in %0.2f second(s)' % (end_time - start_time)

# -----------------------------------------------------------------------------

@app.route('/api/reload', methods=['POST'])
def api_reload():
    profile = request_to_profile(request, profiles_dirs)
    reload(profile)

    return 'Reloaded profile "%s"' % profile.name

def reload(profile):
    # Reset speech recognizer
    global decoders
    decoders.pop(profile.name, None)

    global wake_decoders
    wake_decoders.pop(profile.name, None)

    # Reset intent recognizer
    global intent_projects, intent_examples
    intent_projects.pop(profile.name, None)
    intent_examples.pop(profile.name, None)

    # Reload default profile if necessary
    if profile.name == default_profile_name:
        load_default_profile()

# -----------------------------------------------------------------------------

# Get text from a WAV file
@app.route('/api/speech-to-text', methods=['POST'])
def api_speech_to_text():
    global decoders

    profile = request_to_profile(request, profiles_dirs)

    wav_data = request.data
    decoder = transcribe_wav(profile, wav_data, decoders.get(profile.name))
    decoders[profile.name] = decoder

    if decoder.hyp() is not None:
        return decoder.hyp().hypstr

    return ''

# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------

# Get intent from text
@app.route('/api/text-to-intent', methods=['POST'])
def api_text_to_intent():
    profile = request_to_profile(request, profiles_dirs)

    text = request.data.decode()
    no_hass = request.args.get('nohass', 'false').lower() == 'true'

    start_time = time.time()
    intent = get_intent(profile, text)

    intent_sec = time.time() - start_time
    intent['time_sec'] = intent_sec

    if not no_hass:
        # Send intent to Home Assistant
        utils.send_intent(profile.home_assistant, intent)

    return jsonify(intent)

# -----------------------------------------------------------------------------

# Get intent from a WAV file
@app.route('/api/speech-to-intent', methods=['POST'])
def api_speech_to_intent():
    profile = request_to_profile(request, profiles_dirs)
    wav_data = request.data
    no_hass = request.args.get('nohass', 'false').lower() == 'true'

    start_time = time.time()
    intent = speech_to_intent(profile, wav_data, no_hass)

    intent_sec = time.time() - start_time
    intent['time_sec'] = intent_sec

    if intent is not None:
        return jsonify(intent)

    return jsonify({
        'text': '',
        'intent': '',
        'entities': [],
        'time_sec': intent_sec
    })

# -----------------------------------------------------------------------------

recorder = None

# Start recording a WAV file to a temporary buffer
@app.route('/api/start-recording', methods=['POST'])
def api_start_recording():
    global recorder
    device_index = int(request.args.get('device', -1))
    if device_index < 0:
        device_index = None  # default device

    profile = request_to_profile(request, profiles_dirs)
    recorder = get_audio_recorder(profile)
    recorder.start_recording(device_index)

    return 'OK'

# Stop recording WAV file, transcribe, and get intent
@app.route('/api/stop-recording', methods=['POST'])
def api_stop_recording():
    global decoders, recorder
    no_hass = request.args.get('nohass', 'false').lower() == 'true'

    if recorder is not None:
        record_buffer = recorder.stop_recording()
        logging.debug('Stopped recording (got %s byte(s))' % len(record_buffer))

        profile = request_to_profile(request, profiles_dirs)

        # Convert to WAV
        with io.BytesIO() as wav_data:
            with wave.open(wav_data, mode='wb') as wav_file:
                wav_file.setframerate(16000)
                wav_file.setsampwidth(2)
                wav_file.setnchannels(1)
                wav_file.writeframesraw(record_buffer)

            wav_data.seek(0)
            record_buffer = None

            # speech to text
            decoder = transcribe_wav(profile, wav_data.read(), decoders.get(profile.name))
            decoders[profile.name] = decoder

            # text to intent
            if decoder.hyp() is not None:
                text = decoder.hyp().hypstr
                logging.info(text)
                intent = get_intent(profile, text)

                if not no_hass:
                    # Send intent to Home Assistant
                    utils.send_intent(profile.home_assistant, intent)

                return jsonify(intent)

    return jsonify({
        'text': '',
        'intent': '',
        'entities': []
    })

@app.route('/api/microphones', methods=['GET'])
def api_microphones():
    profile = request_to_profile(request, profiles_dirs)
    mics = get_audio_recorder(profile).get_microphones()
    return jsonify(mics)

def get_audio_recorder(profile):
    system = profile.microphone.get('system', 'pyaudio')
    if system == 'arecord':
        return ARecordAudioRecorder()

    return PyAudioRecorder()

# -----------------------------------------------------------------------------

@app.route('/api/unknown_words', methods=['GET'])
def api_unknown_words():
    profile = request_to_profile(request, profiles_dirs)
    ps_config = profile.speech_to_text['pocketsphinx']

    unknown_words = {}
    unknown_path = profile.read_path(ps_config['unknown_words'])
    if os.path.exists(unknown_path):
        for line in open(unknown_path, 'r'):
            line = line.strip()
            if len(line) > 0:
                word, pronunciation = re.split(r'\s+', line, maxsplit=1)
                unknown_words[word] = pronunciation

    return jsonify(unknown_words)

# -----------------------------------------------------------------------------

@app.errorhandler(Exception)
def handle_error(err):
    logging.exception(err)
    return (str(err), 500)

# ---------------------------------------------------------------------
# Static Routes
# ---------------------------------------------------------------------

@app.route('/css/<path:filename>', methods=['GET'])
def css(filename):
    return send_from_directory(os.path.join(web_dir, 'css'), filename)

@app.route('/js/<path:filename>', methods=['GET'])
def js(filename):
    return send_from_directory(os.path.join(web_dir, 'js'), filename)

@app.route('/img/<path:filename>', methods=['GET'])
def img(filename):
    return send_from_directory(os.path.join(web_dir, 'img'), filename)

@app.route('/webfonts/<path:filename>', methods=['GET'])
def webfonts(filename):
    return send_from_directory(os.path.join(web_dir, 'webfonts'), filename)

# ----------------------------------------------------------------------------
# HTML Page Routes
# ----------------------------------------------------------------------------

@app.route('/', methods=['GET'])
def index():
    return send_file(os.path.join(web_dir, 'index.html'))

@app.route('/swagger.yaml', methods=['GET'])
def swagger_yaml():
    return send_file(os.path.join(web_dir, 'swagger.yaml'))

# -----------------------------------------------------------------------------

# Swagger/OpenAPI documentation
from flask_swagger_ui import get_swaggerui_blueprint

SWAGGER_URL = '/api'
API_URL = '/swagger.yaml'

swaggerui_blueprint = get_swaggerui_blueprint(
    SWAGGER_URL, API_URL,
    config={'app_name': 'Rhasspy API'})

app.register_blueprint(swaggerui_blueprint, url_prefix=SWAGGER_URL)

# -----------------------------------------------------------------------------
