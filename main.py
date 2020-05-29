# -*- coding: utf-8 -*-
import os
import requests
import urllib
import xml.etree.ElementTree as ET
import bs4
from bs4 import BeautifulSoup
from datetime import datetime as dt
import datetime
from retrying import retry
import time
import json
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FollowEvent,
    UnfollowEvent, LocationMessage, LocationSendMessage,
    TemplateSendMessage, MessageAction, ButtonsTemplate,
    URIAction, PostbackAction, PostbackEvent, ConfirmTemplate,
    PostbackTemplateAction
)

# use flask
app = Flask(__name__)

# for LINE API
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# IFTTT webhook
TRIGGER_URL = os.environ["TRIGGER_URL"]

# for COTOHA API, NTT Com. Named Entity Recognition
CLIENT_ID = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]

token_url = "https://api.ce-cotoha.com/v1/oauth/accesstokens"
COTOHA_URL = "https://api.ce-cotoha.com/api/dev/nlp/"

# end_station is your nearest station from your home
end_station = os.environ["end_station"]

def auth(client_id, client_secret):
    """ for COTOHA API auth
    """
    headers = {
        "Content-Type": "application/json",
        "charset": "UTF-8"
    }
    data = {
        "grantType": "client_credentials",
        "clientId": client_id,
        "clientSecret": client_secret
    }
    r = requests.post(token_url,
                      headers=headers,
                      data=json.dumps(data))
    return r.json()["access_token"]

def predict(sentence, access_token):
    """ for COTOHA API, prediction
    """
    base_url = COTOHA_URL
    headers = {
        "Content-Type": "application/json",
        "charset": "UTF-8",
        "Authorization": "Bearer {}".format(access_token)
    }

    data = {
        "sentence": sentence,
    }
    #"ne" means Named Entity Extraction function
    r = requests.post(base_url + "v1/ne",
                      headers=headers,
                      data=json.dumps(data))
    return r.json()

@retry() #to avoid HTTPerror
def do_until_succeed_cotoha(txt, token):
    """ Use retry lib. to avoid HTTPerror
    """
    response = predict(txt, token)
    return response

def cotoha(txt, token = auth(CLIENT_ID, CLIENT_SECRET)):
    """Do Named Entity Extraction against texts using COTOHA API.
    """
    response = do_until_succeed_cotoha(txt, token)
    if [i['std_form'] for i in response['result'] if i['class'] in ['LOC', 'ART']] != []:
        start_station = [i['std_form'] for i in response['result'] if i['class'] in ['LOC', 'ART']][0]
    else:
        start_station = None
    return start_station

@retry
def get_soup(url):
    """ get soup until succeed
    """
    res = requests.get(url ,verify=True)
    res.raise_for_status()
    soup = BeautifulSoup(res.content, "html.parser")
    return soup

def get_minimum_min(start_station):
    """ get minimum minutes to end_station using "Yahoo!乗換案内" without API
    """
    # Use the Yahoo! format
    year, month, day = dt.today().year, str(dt.today().month).zfill(2), str(dt.today().day).zfill(2)
    hour_zfill, min_zfill1, min_zfill2 = str(dt.today().hour).zfill(2), str(dt.today().minute).zfill(2)[1], str(dt.today().minute).zfill(2)[0]

    yahoo_url = 'https://transit.yahoo.co.jp/search/result?flatlon=&fromgid= \
                &from={start_station}&tlatlon=&togid=&to={end_station}&viacode=&via= \
                &viacode=&via=&viacode=&via=&y={year}&m={month}&d={day}\
                &hh={hour_zfill}&m2={min_zfill1}&m1={min_zfill2}&type=1\
                &ticket=ic&expkind=1&ws=2&s=0&kw={end_station}'\
                                        .format(start_station=start_station
                                              , end_station = end_station
                                              , year = year, month = month, day = day
                                              , hour_zfill = hour_zfill
                                              , min_zfill1 = min_zfill1, min_zfill2 = min_zfill2
                                              )

    soup = get_soup(yahoo_url)
    if soup.find(class_="small") != None:
        # dep_time is like '04:55', '05:21'
        dep_time, arr_time = [i.text for i in soup.findAll('li', class_="time")][1][:11].split('→')
        # to datetime
        arr_time = dt.strptime(arr_time, '%H:%M').time()
        dep_time = dt.strptime(dep_time, '%H:%M').time()
        # class, named "small", shows minimum minutes
        time_txt = soup.find(class_="small").text
        # avoid trains runs early morning
        # 17:00 ~ 23:59 or 00:00 ~ 03:00
        if ((dep_time > datetime.time(17, 0)) & (dep_time < datetime.time(23, 59))) | ((dep_time > datetime.time(0, 0)) & (dep_time < datetime.time(3, 0))):
            if '時' in time_txt:
                hour , min_ = [int(i) for i in time_txt.replace('分', '').split('時間')]
                minimum_min = hour * 60 + min_
            else:
                minimum_min = int(soup.find(class_="small").text.replace('分', ''))
        else:
            minimum_min = None
    else:
        # if you are at the nearest station
        minimum_min = 0.1
    return minimum_min

def ctr(user_id, start_station, min_duration):
    minimum_min = get_minimum_min(start_station)
    if minimum_min:
        start_dt = dt.today() + datetime.timedelta(minutes=minimum_min)
        start_dt = str(start_dt.hour).zfill(2) + ':' + str(start_dt.minute).zfill(2)
        end_dt = dt.today() + datetime.timedelta(minutes=minimum_min + min_duration)
        end_dt = str(end_dt.hour).zfill(2) + ':' + str(end_dt.minute).zfill(2)
        reply_text = "{start_dt}から{end_dt}までライトを点灯します。".format(start_dt=start_dt, end_dt=end_dt)
        line_bot_api.push_message(user_id, TextSendMessage(text=reply_text))

        # turn the light on via IFTTT
        time.sleep(minimum_min * 60)
        requests.post(TRIGGER_URL)

        # turn the light off via IFTTT
        time.sleep(min_duration * 60)
        requests.post(TRIGGER_URL)
    else:
        line_bot_api.push_message(user_id
                    , TextSendMessage(text='終電が過ぎています。'))

@app.route("/")
def hello_world():
    """ print on webhook for testing
    """
    return "hello world!"

@app.route("/callback", methods=['POST'])
def callback():
    """ callback
    """
    signature = request.headers['X-Line-Signature']
    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    # handle webhook body
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=LocationMessage)
def handle_location(event):
    lat, lon = event.message.latitude, event.message.longitude

    # get nearest stations form ur home via Simple API
    # reference: http://rautaku.hatenablog.com/entry/2018/01/07/153000
    near_station_url = 'http://map.simpleapi.net/stationapi?x={}&y={}&output=xml'.format(lon, lat)
    near_station_req = urllib.request.Request(near_station_url)

    with urllib.request.urlopen(near_station_req) as response:
        near_station_XmlData = response.read()

    near_station_root = ET.fromstring(near_station_XmlData)
    near_station_list = near_station_root.findall(".//name")
    near_station_list_jpn = [i.text for i in near_station_list]

    if ('駅' in end_station) & (end_station in near_station_list_jpn):
        near_station_list_jpn.remove(end_station)
    elif ('駅' not in end_station) & (end_station in near_station_list_jpn):
        near_station_list_jpn.remove(end_station + '駅')
    else:
        pass

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text='お疲れ様です。{}からお帰りですね。'.format(near_station_list_jpn[0])))
    profile = line_bot_api.get_profile(event.source.user_id)
    user_id = profile.user_id

    ctr(user_id, near_station_list_jpn[0], 30)

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    start_station = cotoha(event.message.text)
    if start_station:
        profile = line_bot_api.get_profile(event.source.user_id)
        user_id = profile.user_id
        ctr(user_id, start_station, 30)
    else:
        reply_text = "もう一度駅名を入力してください。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
   port = int(os.getenv("PORT"))
   app.run(host="0.0.0.0", port=port)