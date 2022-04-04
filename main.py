from flask import Flask, request, redirect, make_response, render_template
import requests, websocket
from requests.auth import HTTPBasicAuth
import json, time, random
from datetime import datetime
from PIL import Image
from io import BytesIO
import numpy as np
import glob
app = Flask(__name__)


# const strings
with open('secrets.json', 'r') as f:
  secrets = json.loads(f.read())
client_id = secrets['client_id']
client_secret = secrets['client_secret']
redirect_uri = secrets['redirect_uri']
state = 'tartans'
color_str = '''6D001A
BE0039
FF4500
FFA800
FFD635
FFF8B8
00A368
00CC78
7EED56
00756F
009EAA
00CCC0
2450A4
3690EA
51E9F4
493AC1
6A5CFF
94B3FF
811E9F
B44AC0
E4ABFF
DE107F
FF3881
FF99AA
6D482F
9C6926
FFB470
000000
515252
898D90
D4D7D9
FFFFFF'''
color_map = np.zeros((32,3), dtype=int)
for i, s in enumerate(color_str.split()):
  for j in range(3):
    color_map[i][j] = int(s[2*j:2*j+2],16)
def get_closest_color(rgb):
  return int(np.argmin(np.sum(np.abs(color_map-rgb), axis=1)))


# global variables
last_placed = 0
last_checked_board = 0
messages = []
fix_queue = []
online_users = {}


@app.route('/')
def index():
  maintaining = []
  for imgpath, idxpath in zip(sorted(glob.glob('static/*.png')), sorted(glob.glob('static/*.txt'))):
    d = {}
    d['path'] = imgpath
    with open(idxpath, 'r') as f:
      d['x'], d['y'] = tuple(map(int, f.readline().split()))
    maintaining.append(d)
  return render_template('template.html', maintaining=maintaining)


@app.route('/logout')
def logout():
  response = make_response(redirect('/'))
  response.set_cookie('token', '', expires=0)
  response.set_cookie('refresh', '', expires=0)
  response.set_cookie('username', '', expires=0)
  return response


@app.route('/authorize')
def authorize():
  # first visit
  if len(request.args) == 0:
    reddit_auth_url = f'https://www.reddit.com/api/v1/authorize?client_id={client_id}&response_type=code&state={state}&redirect_uri={redirect_uri}&duration=permanent&scope=identity'
    return redirect(reddit_auth_url)

  # returned error from reddit oauth
  print(request.args)
  if 'error' in request.args:
    return 'error: ' + request.args['error']

  # succeeded from reddit oauth
  if 'code' not in request.args or 'state' not in request.args or request.args['state'] != state:
    return 'ERROR. Please contact Trollium on discord.'

  # obtain bearer token
  tokencode = request.args['code']
  r = requests.post('https://www.reddit.com/api/v1/access_token',
                    auth=HTTPBasicAuth(client_id, client_secret),
                    headers={'user-agent': 'trolliumbot/0.1'},
                    data={'grant_type': 'authorization_code', 'code': tokencode, 'redirect_uri': redirect_uri})
  print(r.text)
  if r.status_code == 429:
    return 'rate limit reached, please <a href="http://cmuplace.trollium.tk/authorize">try again</a> in a few seconds.'
  if r.status_code != 200:
    return 'ERROR. Please contact Trollium on discord.'
  json_response = r.json()
  access_token = json_response['access_token']
  expires_in = json_response['expires_in']
  refresh_token = json_response['refresh_token']

  # complete authorization by setting cookies
  response = make_response(redirect('/'))
  response.set_cookie('token', access_token, max_age=expires_in)
  response.set_cookie('refresh', refresh_token, max_age=86400)

  # obtain username
  r = requests.get('https://oauth.reddit.com/api/v1/me',
                   headers={'authorization': 'Bearer '+access_token, 'user-agent': 'trolliumbot/0.1'})
  if r.status_code == 429:
    return 'rate limit reached, please <a href="http://cmuplace.trollium.tk/authorize">try again</a> in a few seconds.'
  if r.status_code != 200:
    return 'ERROR. Please contact Trollium on discord.'
  response.set_cookie('username', r.json()['name'], max_age=86400)

  return response


@app.route('/info')
def info():
  global last_checked_board, online_users

  new_token = None
  # refresh token if necessary
  if 'token' not in request.cookies and 'refresh' in request.cookies:
    r = requests.post('https://www.reddit.com/api/v1/access_token',
                      auth=HTTPBasicAuth(client_id, client_secret),
                      headers={'user-agent': 'trolliumbot/0.1'},
                      data={'grant_type': 'refresh_token', 'refresh_token': request.cookies['refresh']})
    print(r.text)
    if r.status_code == 200:
      json_response = r.json()
      new_token = (json_response['access_token'], json_response['expires_in'])

  # check for board accuracy
  if time.time() - last_checked_board > 10 and 'token' in request.cookies:
    refresh_fix_queue(request.cookies['token'])
    last_checked_board = time.time()

  # log online users
  if 'username' in request.cookies and 'token' in request.cookies:
    online_users[request.cookies['username']] = time.time()
  need_deletion = []
  for k, v in online_users.items():
    if time.time() - v > 20:
      need_deletion.append(k)
  for k in need_deletion:
    del online_users[k]

  sb = f'{len(online_users)} users online.<br/>'
  sb += '<br/>'.join(messages[::-1])
  sb += f'<br/>Detected {len(fix_queue)} differences: '
  sb += str(fix_queue)
  
  response = make_response(sb)
  if new_token is not None:
    response.set_cookie('token', new_token[0], max_age=new_token[1])
  return response


@app.route('/ready')
def ready():
  global last_placed, messages
  if time.time() - last_placed < 5: return ''
  if len(fix_queue) == 0: return ''

  username = request.cookies['username']
  x, y, color = fix_queue[-1]
  del fix_queue[-1]
  success, readytime = placetile(request.cookies['token'], x, y, color)

  if success:
    currtimestr = datetime.now().strftime('%H:%M:%S')
    newmessage = f'{currtimestr}: {username} placed {color} at ({x},{y})'
    messages.append(newmessage)
    last_placed = time.time()
    return newmessage + '<br/>\n' + str(readytime)
  elif readytime != -1:
    return '\n' + str(readytime)
  else:
    return ''


def placetile(token, x, y, color):
  payload = json.dumps({
    'operationName': 'setPixel',
    'variables': {
      'input': {
        'actionName': 'r/replace:set_pixel',
        'PixelMessageData': {
          'coordinate': {
            'x': x % 1000,
            'y': y % 1000
          },
          'colorIndex': color,
          'canvasIndex': x//1000 + (y//1000) * 2
        }
      }
    },
    'query': 'mutation setPixel($input: ActInput!) {\n  act(input: $input) {\n    data {\n      ... on BasicMessage {\n        id\n        data {\n          ... on GetUserCooldownResponseMessageData {\n            nextAvailablePixelTimestamp\n            __typename\n          }\n          ... on SetPixelResponseMessageData {\n            timestamp\n            __typename\n          }\n          __typename\n        }\n        __typename\n      }\n      __typename\n    }\n    __typename\n  }\n}\n'
  })
  headers = {
      'origin': 'https://hot-potato.reddit.com',
      'referer': 'https://hot-potato.reddit.com/',
      'apollographql-client-name': 'mona-lisa',
      'Authorization': 'Bearer ' + token,
      'Content-Type': 'application/json'
  }
  r = requests.request('POST', 'https://gql-realtime-2.reddit.com/query', headers=headers, data=payload)
  print(r.text)
  if r.status_code == 200:
    j = r.json()
    if 'errors' in j:
      readytime = j['errors'][0]['extensions']['nextAvailablePixelTs']//1000
      success = False
    else:
      readytime = j['data']['act']['data'][0]['data']['nextAvailablePixelTimestamp']//1000
      success = True
  else:
    readytime = -1
    success = False
  print(success, readytime)
  return (success, readytime)


def get_board_img(bearer):
  ws = websocket.WebSocket()
  ws.connect("wss://gql-realtime-2.reddit.com/query")
  ws.send(json.dumps({"type":"connection_init","payload":{"Authorization":"Bearer "+bearer}}))
  ws.recv()
  ws.send(json.dumps({"id":"1","type":"start","payload":{"variables":{"input":{"channel":{"teamOwner":"AFD2022","category":"CONFIG"}}},"extensions":{},"operationName":"configuration","query":"subscription configuration($input: SubscribeInput!) {\n  subscribe(input: $input) {\n    id\n    ... on BasicMessage {\n      data {\n        __typename\n        ... on ConfigurationMessageData {\n          colorPalette {\n            colors {\n              hex\n              index\n              __typename\n            }\n            __typename\n          }\n          canvasConfigurations {\n            index\n            dx\n            dy\n            __typename\n          }\n          canvasWidth\n          canvasHeight\n          __typename\n        }\n      }\n      __typename\n    }\n    __typename\n  }\n}\n"}}))
  ws.recv()

  imgs = [None]*4
  already_added = []
  for i in range(4):
    ws.send(json.dumps({"id":str(2+i),"type":"start","payload":{"variables":{"input":{"channel":{"teamOwner":"AFD2022","category":"CANVAS","tag":str(i)}}},"extensions":{},"operationName":"replace","query":"subscription replace($input: SubscribeInput!) {\n  subscribe(input: $input) {\n    id\n    ... on BasicMessage {\n      data {\n        __typename\n        ... on FullFrameMessageData {\n          __typename\n          name\n          timestamp\n        }\n        ... on DiffFrameMessageData {\n          __typename\n          name\n          currentTimestamp\n          previousTimestamp\n        }\n      }\n      __typename\n    }\n    __typename\n  }\n}\n"}}))
  while any(x is None for x in imgs):
    msg = json.loads(ws.recv())
    if msg['type'] == 'data' and msg['payload']['data']['subscribe']['data']['__typename'] == 'FullFrameMessageData':
      url = msg['payload']['data']['subscribe']['data']['name']
      imgs[int(msg['id'])-2] = Image.open(BytesIO(requests.get(url, stream=True).content))
      ws.send(json.dumps({"id":msg['id'],"type":"stop"}))
  ws.close()

  new_im = Image.new('RGB', (2000, 2000))
  for i in range(len(imgs)):
    new_im.paste(imgs[i], (i%2*1000, i//2*1000))
  return new_im

def refresh_fix_queue(bearer):
  global fix_queue
  fix_queue = []
  curr_board = np.array(get_board_img(bearer))
  for imgpath, idxpath in zip(sorted(glob.glob('static/*.png')), sorted(glob.glob('static/*.txt'))):
    need_fixes = []
    pilimg = Image.open(imgpath).convert('RGBA')
    npimg = np.array(pilimg).astype(int)
    ylen, xlen, channels = npimg.shape
    with open(idxpath, 'r') as f:
      xstart, ystart = tuple(map(int, f.readline().split()))
    boardpatch = curr_board[ystart:ystart+ylen, xstart:xstart+xlen]
    for y in range(ylen):
      for x in range(xlen):
        if channels <= 3 or npimg[y][x][-1] == 255:
          boardcol = get_closest_color(boardpatch[y][x][:3])
          imgcol = get_closest_color(npimg[y][x][:3])
          if boardcol != imgcol:
            need_fixes.append((x+xstart, y+ystart, imgcol))
    #random.shuffle(need_fixes)
    fix_queue.extend(need_fixes)




if __name__ == '__main__':
  app.run(host='0.0.0.0', port=80)