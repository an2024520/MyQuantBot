# app/routes/views.py
from flask import Blueprint, render_template

bp = Blueprint('views', __name__)

@bp.route('/')
def dashboard():
    return render_template('dashboard.html')

@bp.route('/future_grid_panel')
def future_grid_panel():
    return render_template('future_grid_bot.html')
    
@bp.route('/chart/<string:symbol_slug>')
def chart_page(symbol_slug):
    # symbol_slug 可能是 "BTC_USDT"，前端自己会解析
    return render_template('chart.html')

@bp.route('/autopilot')
def autopilot():
    return render_template('autopilot.html')