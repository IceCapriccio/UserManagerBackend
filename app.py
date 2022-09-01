from flask import Flask, render_template
from gpustatweb import gpu_query_start
app = Flask(__name__)


@app.route('/')
def home():
    return render_template('index.html', name='name')

@app.route('/adduser')
def adduser():
    pass


if __name__ == '__main__':
    app.run()
