from flask import Flask, g

import core

app = Flask(__name__)


@app.route('/')
def index():
    g.db = core.connect('notes.db')
    return str(core.count(g.db, 'me'))
