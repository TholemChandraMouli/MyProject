import matplotlib
matplotlib.use('Agg') # Use 'Agg' backend for non-interactive plotting
import matplotlib.pyplot as plt
import io
import base64
from flask import Flask, render_template, jsonify, request, make_response, send_file
import requests
import json
import time
import threading
import os
import finnhub # Keep finnhub for the dashboard part if desired
import pandas as pd
import yfinance as yf # Import yfinance for DRIP calculator data
from datetime import datetime, timedelta, timezone # Import timezone

# For PDF generation
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

app = Flask(__name__)

# --- Configuration for Finnhub (Dashboard) ---
# It's highly recommended to set this as an environment variable in production
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "d1nlrb1r01qovv8k2q6gd1nlrb1r01qovv8k2q70") 

finnhub_client = finnhub.Client(api_key=FINNHUB_API_KEY)

# Default stock symbols for the dashboard display
STOCK_SYMBOLS = [ "AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "TSLA", "IBM", "META", "JPM", "KO", "PG", "UNH", "VOO", "SPY",
    "INTC", "PEP", "V", "MA", "DIS", "NFLX", "ADBE", "CRM", "ORCL", "CSCO", "BA", "WMT", "CVX", "XOM",
    "BAC", "T", "NKE", "MCD", "HD", "PFE", "MRK", "ABT", "TMO", "LLY", "COST", "AVGO", "GE", "DHR",
    "BMY", "CAT", "QCOM", "AMAT", "AMD", "FDX", "UPS", "GILD", "AXP", "DE", "BKNG", "ZTS"]
FETCH_INTERVAL_SECONDS = 30 # How often to refresh dashboard data
latest_stock_data = {} # Stores the latest stock data for the dashboard
data_lock = threading.Lock() # Lock for thread-safe access to latest_stock_data

def fetch_and_update_dashboard_stock_data(symbol, api_key):
    """
    Fetches real-time stock data for the dashboard using Finnhub API.
    Updates the global latest_stock_data dictionary.
    """
    quote_url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={api_key}"
    company_profile_url = f"https://finnhub.io/api/v1/stock/profile2?symbol={symbol}&token={api_key}"

    try:
        quote_response = requests.get(quote_url)
        quote_response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        quote_data = quote_response.json()

        profile_response = requests.get(company_profile_url)
        profile_response.raise_for_status()
        profile_data = profile_response.json()

        # Check if current price data is available
        if not quote_data or quote_data.get('c') is None:
            print(f"No valid quote data for {symbol}")
            return

        company_name = profile_data.get('name', symbol)
        
        current_price = quote_data.get('c')
        high_price = quote_data.get('h')
        low_price = quote_data.get('l')
        open_price = quote_data.get('o')
        prev_close_price = quote_data.get('pc')

        change = current_price - prev_close_price
        percentage_change = (change / prev_close_price * 100) if prev_close_price else 0

        with data_lock: # Ensure thread safety when updating shared data
            latest_stock_data[symbol] = {
                "symbol": symbol,
                "company_name": company_name,
                "logo": profile_data.get('logo', ''),
                "current_price": f"{current_price:.2f}",
                "high_price": f"{high_price:.2f}",
                "low_price": f"{low_price:.2f}",
                "open_price": f"{open_price:.2f}",
                "prev_close_price": f"{prev_close_price:.2f}",
                "change": f"{change:.2f}",
                "percentage_change": f"{percentage_change:.2f}",
                "timestamp": int(time.time() * 1000) # Timestamp in milliseconds
            }
            print(f"Updated dashboard data for {symbol} ({company_name})")

    except requests.exceptions.RequestException as req_err:
        print(f"Network or API error fetching dashboard data for {symbol}: {req_err}")
    except json.JSONDecodeError as json_err:
        print(f"JSON decoding error for {symbol}: {json_err}")
    except Exception as e:
        print(f"An unexpected error occurred fetching dashboard data for {symbol}: {e}")

def background_data_updater():
    """
    Background thread function to periodically fetch and update stock data for the dashboard.
    """
    while True:
        start_time = time.time()
        for symbol in STOCK_SYMBOLS:
            fetch_and_update_dashboard_stock_data(symbol, FINNHUB_API_KEY)
            time.sleep(0.5) # Small delay to respect API limits if many symbols are fetched in sequence
        
        elapsed_time = time.time() - start_time
        time_to_sleep = FETCH_INTERVAL_SECONDS - elapsed_time
        if time_to_sleep > 0:
            time.sleep(time_to_sleep)

# Start the background data updater thread when the application starts
updater_thread = threading.Thread(target=background_data_updater, daemon=True)
updater_thread.start()

# --- Helper Function for DRIP Calculator Data Fetching (using yfinance) ---
def get_drip_stock_data(ticker_symbol):
    """
    Fetches comprehensive stock and dividend data for DRIP calculation using yfinance.
    Returns a dictionary with relevant data or an error dictionary if an error occurs.
    
    NOTE: Removed payout_frequency inference here to allow user input to take precedence.
    """
    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info

        # Check if ticker info is valid/available at all
        if not info:
            return {"error": f"No information found for ticker: {ticker_symbol}. It might be an invalid symbol or data is unavailable from yfinance."}

        current_price = info.get('currentPrice')
        # Specific check for currentPrice as it's critical for calculations
        if current_price is None:
            return {"error": f"Current price data not available for {ticker_symbol}. Data might be delayed or unavailable from yfinance."}

        long_name = info.get('longName') or ticker_symbol

        # Attempt to get dividend yield from various fields
        dividend_yield = info.get('dividendYield') 
        if dividend_yield is None:
            dividend_yield = info.get('forwardAnnualDividendYield')
        # Calculate yield if dividendRate and currentPrice are available but yield is not
        if dividend_yield is None and info.get('dividendRate') and current_price:
            dividend_yield = info['dividendRate'] / current_price
        
        # Annual Dividend Rate (per share)
        annual_dividend_rate_per_share = info.get('dividendRate')

        # Historical Dividends for growth calculation and chart
        # Ensure datetime is used correctly here, making it timezone-aware for comparison
        # yfinance dates are typically timezone-aware, so make end_date and start_date_5yr tz-aware too
        end_date = datetime.now(timezone.utc) # Make end_date timezone-aware (UTC)
        start_date_5yr = end_date - timedelta(days=5 * 365) # Look back 5 years for growth calculation
        
        # Filter historical_dividends_series by comparing tz-aware objects
        # Convert index to timezone-aware if it's not already (yfinance usually makes it tz-aware)
        historical_dividends_series = ticker.dividends.loc[start_date_5yr:end_date]
        
        annual_dividends_by_year = {}
        for date, dividend_amount in historical_dividends_series.items():
            year = date.year
            # Corrected: Use annual_dividends_by_year dictionary here
            annual_dividends_by_year[year] = annual_dividends_by_year.get(year, 0) + dividend_amount
        
        sorted_years = sorted(annual_dividends_by_year.keys())
        
        dividend_growth_rate = 0.0 # Default to 0 if not enough data
        
        # Calculate 5-year Compound Annual Growth Rate (CAGR) for dividends
        if len(sorted_years) >= 2: # Need at least two years for growth
            # Find the dividend amount for the earliest and latest year in the 5-year window
            first_year_div = annual_dividends_by_year.get(sorted_years[0], 0)
            last_year_div = annual_dividends_by_year.get(sorted_years[-1], 0)
            num_years = sorted_years[-1] - sorted_years[0]

            if first_year_div > 0 and num_years > 0:
                dividend_growth_rate = ((last_year_div / first_year_div) ** (1 / num_years)) - 1
            elif last_year_div > 0 and first_year_div == 0:
                # If dividends started within the period, consider it high growth or handle as appropriate
                # For simplicity, if first year was 0 and last year is > 0, we can't calculate CAGR directly.
                # Could set a default high growth or indicate it started paying dividends.
                # For now, keep it 0 as a safe default if CAGR formula fails.
                dividend_growth_rate = 0.0 # Or a more suitable default/indicator
            
        # Historical prices for charts (last 5 years)
        hist_prices_df = ticker.history(period="5y") 
        
        last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        return {
            "ticker": ticker_symbol,
            "longName": long_name,
            "currentPrice": current_price,
            "dividendYield": round(dividend_yield * 100, 2) if dividend_yield is not None else 0.0,
            "annualDividendRate": round(annual_dividend_rate_per_share, 2) if annual_dividend_rate_per_share is not None else 0.0,
            "annualDividendGrowthRate": round(dividend_growth_rate * 100, 2) if dividend_growth_rate is not None else 0.0,
            "payoutFrequency": "N/A", # This will now reflect the default, as user input will override for calculations
            "historicalDividendsSeries": historical_dividends_series, # Raw series for plotting
            "annualDividendsByYear": annual_dividends_by_year, # Aggregated for growth calc
            "historicalPrices": hist_prices_df, # DataFrame for plotting
            "lastUpdated": last_updated # Ensure this is always set
        }
    except Exception as e:
        print(f"Error fetching DRIP data for {ticker_symbol}: {e}")
        # Provide a more informative error message and ensure lastUpdated is present
        return {"error": f"Failed to fetch data for {ticker_symbol}. Reason: {e}. Please check the symbol or try again later.", "lastUpdated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

# --- Flask Routes ---
@app.route('/')
def dashboard():
    """Renders the main dashboard page."""
    return render_template('index.html')

@app.route('/api/stocks')
def api_stocks():
    """Provides the latest stock data for the dashboard as JSON."""
    with data_lock:
        return jsonify(latest_stock_data)
        
@app.route('/calculator', methods=['GET', 'POST'])
def calculator():
    """Renders the dividend calculator page (GET) and handles the dividend calculation (POST)."""
    results = []
    comparison_table = None
    
    # Default values for form inputs (used for GET request or if POST values are invalid)
    initial_investment = 10000.0
    investment_years = 10
    drip_enabled = 'yes' # Default to checked
    selected_tickers = ["NVDA"] # Default selected ticker for initial load
    initial_share_price = 100.0
    dividend_yield = 2.5
    annual_dividend_growth_rate = 5.0
    annual_stock_price_growth_rate = 7.0
    payout_frequency = "Quarterly" # Default payout frequency for GET request

    if request.method == 'POST':
        # Retrieve form data, converting to appropriate types
        initial_investment = request.form.get('initial_investment', type=float)
        investment_years = request.form.get('investment_years', type=int)
        drip_enabled = request.form.get('drip_enabled') # 'yes' or 'no'
        selected_tickers = request.form.getlist('tickers') # List of selected tickers
        initial_share_price = request.form.get('initial_share_price', type=float)
        dividend_yield = request.form.get('dividend_yield', type=float)
        annual_dividend_growth_rate = request.form.get('annual_dividend_growth_rate', type=float)
        annual_stock_price_growth_rate = request.form.get('annual_stock_price_growth_rate', type=float)
        # IMPORTANT: Use the payout_frequency from the form submission for calculations and display
        payout_frequency = request.form.get('payout_frequency') 

        # --- Debug Print: Payout Frequency from Form ---
        print(f"\n--- Calculation for new request ---")
        print(f"Payout Frequency from form: {payout_frequency}")

        # Input validation for numerical fields
        if not initial_investment or initial_investment <= 0:
            results.append({'error': 'Please provide a positive initial investment.'})
        if not investment_years or investment_years <= 0:
            results.append({'error': 'Please provide a positive investment duration.'})
        if not selected_tickers:
            results.append({'error': 'Please select at least one ticker symbol.'})
        if not initial_share_price or initial_share_price <= 0:
            results.append({'error': 'Please provide a positive initial share price.'})
        if dividend_yield is None or dividend_yield < 0:
            results.append({'error': 'Please provide a non-negative dividend yield.'})
        if annual_dividend_growth_rate is None:
            results.append({'error': 'Please provide an annual dividend growth rate.'})
        if annual_stock_price_growth_rate is None:
            results.append({'error': 'Please provide an annual stock price growth rate.'})
        if not payout_frequency: # Validate payout frequency
             results.append({'error': 'Please select a dividend payout frequency.'})


        if not results: # Only proceed with calculations if no validation errors so far
            comparison_data_for_table = [] # To collect data for the comparison table
            
            for ticker_symbol in selected_tickers:
                ticker_symbol = ticker_symbol.strip().upper()
                if not ticker_symbol:
                    continue

                drip_data = get_drip_stock_data(ticker_symbol)
                if "error" in drip_data:
                    # Ensure last_updated is still passed even with an error
                    results.append({'ticker': ticker_symbol, 'error': drip_data['error'], 'last_updated': drip_data.get('lastUpdated', 'N/A')})
                    continue
                
                # Use fetched data if available, otherwise fall back to user input defaults
                current_price_calc = drip_data['currentPrice'] if drip_data['currentPrice'] is not None else initial_share_price
                div_yield_decimal_calc = (drip_data['dividendYield'] / 100) if drip_data['dividendYield'] is not None else (dividend_yield / 100)
                div_growth_decimal_calc = (drip_data['annualDividendGrowthRate'] / 100) if drip_data['annualDividendGrowthRate'] is not None else (annual_dividend_growth_rate / 100)
                stock_price_growth_decimal_calc = (annual_stock_price_growth_rate / 100)
                
                annual_dividend_per_share_start = current_price_calc * div_yield_decimal_calc # Initial annual dividend per share
                
                # Handle cases where dividend data might be zero
                if annual_dividend_per_share_start == 0:
                     results.append({
                        'ticker': ticker_symbol,
                        'current_price': f"${current_price_calc:,.2f}",
                        'error': f"{ticker_symbol} does not currently pay dividends. DRIP has no effect.",
                        'last_updated': drip_data.get('lastUpdated', 'N/A') # Ensure last_updated is here too
                    })
                     continue

                # Initialize simulation variables
                current_shares_drip = initial_investment / current_price_calc
                current_shares_no_drip = initial_investment / current_price_calc # Shares remain constant for no DRIP
                
                current_stock_price_sim = current_price_calc # Stock price changes over time for both scenarios
                annual_dividend_per_share_sim = annual_dividend_per_share_start # Annual dividend per share changes over time

                total_dividends_reinvested_drip = 0
                total_dividends_received_no_drip = 0 # For 'No DRIP' scenario

                yearly_breakdown_for_display = []
                portfolio_values_with_drip_chart = [] # Use for Chart.js
                shares_owned_with_drip_chart = [] # Use for Chart.js
                years_for_chart = [] # Years for Chart.js x-axis

                # Initial data point for charts
                portfolio_values_with_drip_chart.append({'x': 0, 'y': initial_investment})
                shares_owned_with_drip_chart.append({'x': 0, 'y': initial_investment / current_price_calc})
                years_for_chart.append(0) # Year 0 for initial investment

                # --- Debug Print: Initial State ---
                print(f"\n--- Simulating {ticker_symbol} with Payout Frequency: {payout_frequency} ---")
                print(f"Initial Investment: ${initial_investment:,.2f}, Initial Shares: {current_shares_drip:,.2f}")

                for year in range(1, investment_years + 1):
                    # Values at the beginning of the current year
                    shares_at_start_of_year_drip = current_shares_drip
                    shares_at_start_of_year_no_drip = current_shares_no_drip # This remains constant
                    stock_price_at_start_of_year = current_stock_price_sim
                    annual_dividend_per_share_at_start_of_year = annual_dividend_per_share_sim

                    dividends_received_this_year_drip = 0
                    reinvested_shares_this_year_drip = 0

                    dividends_received_this_year_no_drip = shares_at_start_of_year_no_drip * annual_dividend_per_share_at_start_of_year
                    total_dividends_received_no_drip += dividends_received_this_year_no_drip

                    # Use the user-selected payout_frequency for calculation
                    number_of_payouts = {
                        'Annual': 1,
                        'Semi-Annual': 2,
                        'Quarterly': 4,
                        'Monthly': 12
                    }.get(payout_frequency, 1)

                    # Calculate period growth rates
                    # This is the key change: distribute annual growth over payout periods
                    period_stock_price_growth_rate = (1 + stock_price_growth_decimal_calc) ** (1/number_of_payouts) - 1
                    period_dividend_growth_rate = (1 + div_growth_decimal_calc) ** (1/number_of_payouts) - 1

                    # --- Debug Print: Number of Payouts ---
                    if year == 1: # Only print once per ticker
                        print(f"Number of payouts per year: {number_of_payouts}")
                        print(f"Period Stock Price Growth Rate: {period_stock_price_growth_rate:.4f}")
                        print(f"Period Dividend Growth Rate: {period_dividend_growth_rate:.4f}")


                    # Simulate payouts within the year for DRIP
                    if drip_enabled == 'yes':
                        for i in range(number_of_payouts):
                            # Update stock price and dividend per share for the current period
                            # The stock price for reinvestment should reflect growth up to this period
                            current_stock_price_for_reinvestment = stock_price_at_start_of_year * ((1 + period_stock_price_growth_rate) ** i)
                            current_dividend_per_share_for_period = (annual_dividend_per_share_at_start_of_year / number_of_payouts) * ((1 + period_dividend_growth_rate) ** i)


                            dividends_received_this_period_drip = shares_at_start_of_year_drip * current_dividend_per_share_for_period
                            
                            shares_bought_this_period = 0
                            if current_stock_price_for_reinvestment > 0: # Avoid division by zero
                                shares_bought_this_period = dividends_received_this_period_drip / current_stock_price_for_reinvestment
                            
                            current_shares_drip += shares_bought_this_period
                            dividends_received_this_year_drip += dividends_received_this_period_drip
                            reinvested_shares_this_year_drip += shares_bought_this_period
                        total_dividends_reinvested_drip += dividends_received_this_year_drip
                    else:
                        # If DRIP is not enabled, dividends are received but not reinvested into shares
                        dividends_received_this_year_drip = shares_at_start_of_year_drip * annual_dividend_per_share_at_start_of_year
                        reinvested_shares_this_year_drip = 0 # No shares reinvested
                        total_dividends_reinvested_drip += dividends_received_this_year_drip # Accumulate for total display
                    
                    # Update stock price and annual dividend per share for the *end* of the year
                    # This is the price at which the portfolio value is calculated for the end of the year
                    # and also the starting price for the next year.
                    current_stock_price_sim = stock_price_at_start_of_year * (1 + stock_price_growth_decimal_calc)
                    annual_dividend_per_share_sim = annual_dividend_per_share_at_start_of_year * (1 + div_growth_decimal_calc)

                    # Calculate portfolio values at the end of the year
                    portfolio_value_end_of_year_drip = current_shares_drip * current_stock_price_sim
                    
                    # For No DRIP: value is initial shares * current stock price + accumulated dividends (not reinvested)
                    # This needs to be calculated based on the fixed initial shares and the current stock price
                    portfolio_value_end_of_year_no_drip = (initial_investment / initial_share_price) * current_stock_price_sim + total_dividends_received_no_drip

                    portfolio_values_with_drip_chart.append({'x': year, 'y': portfolio_value_end_of_year_drip})
                    shares_owned_with_drip_chart.append({'x': year, 'y': current_shares_drip})
                    years_for_chart.append(year)

                    # Store data for this year for display table
                    yearly_breakdown_for_display.append({
                        'Year': year,
                        'Shares Owned (Start)': f"{shares_at_start_of_year_drip:,.2f}", # DRIP shares start of year
                        'Stock Price (Start)': f"${stock_price_at_start_of_year:,.2f}",
                        'Annual Dividend Per Share': f"${annual_dividend_per_share_at_start_of_year:,.2f}",
                        'Dividends Received (Year)': f"${dividends_received_this_year_drip:,.2f}", # Dividends generated this year
                        'Reinvested Shares (Year)': f"{reinvested_shares_this_year_drip:,.2f}",
                        'Shares Owned (End)': f"{current_shares_drip:,.2f}", # DRIP shares end of year
                        'Portfolio Value (End)': f"${portfolio_value_end_of_year_drip:,.2f}" # DRIP portfolio value
                    })

                    # --- Debug Print: Yearly Breakdown ---
                    print(f"Year {year}: Shares Owned (End) DRIP: {current_shares_drip:,.2f}, Portfolio Value (End) DRIP: ${portfolio_value_end_of_year_drip:,.2f}")


                # Final values after loop
                final_investment_value_with_drip = portfolio_values_with_drip_chart[-1]['y']
                final_shares_with_drip = shares_owned_with_drip_chart[-1]['y']

                # Correct calculation for final_value_no_drip and final_shares_no_drip
                final_shares_no_drip = initial_investment / initial_share_price # Shares remain constant
                final_stock_price_at_end = current_price_calc * ((1 + stock_price_growth_decimal_calc) ** investment_years)
                final_value_no_drip = (initial_investment / initial_share_price) * final_stock_price_at_end + total_dividends_received_no_drip


                # Calculate Yield on Cost
                yield_on_cost = 0.0
                if initial_share_price > 0 and annual_dividend_per_share_start > 0:
                    yield_on_cost = (annual_dividend_per_share_start / initial_share_price) * 100

                # Prepare data for Chart.js
                historical_dividend_chart_data = []
                if drip_data['historicalDividendsSeries'] is not None and not drip_data['historicalDividendsSeries'].empty:
                    # Resample to annual for the plot, if not already annual
                    annual_div_plot_series = drip_data['historicalDividendsSeries'].resample('Y').sum()
                    for date, amount in annual_div_plot_series.items():
                        historical_dividend_chart_data.append({'x': date.strftime('%Y-%m-%d'), 'y': round(amount, 2)})
                
                historical_price_chart_data = []
                if drip_data['historicalPrices'] is not None and not drip_data['historicalPrices'].empty:
                    for date, price in drip_data['historicalPrices']['Close'].items():
                        historical_price_chart_data.append({'x': date.strftime('%Y-%m-%d'), 'y': round(price, 2)})

                results.append({
                    'ticker': ticker_symbol,
                    'current_price': f"${drip_data['currentPrice'] if drip_data['currentPrice'] is not None else initial_share_price:,.2f}",
                    'annual_dividend_per_share': f"${annual_dividend_per_share_start:,.2f}",
                    'dividend_yield': f"{drip_data['dividendYield'] if drip_data['dividendYield'] is not None else dividend_yield:,.2f}%",
                    'payout_ratio': f"{drip_data.get('payoutRatioTTM', 'N/A')}", # yfinance doesn't provide payoutRatioTTM directly in info()
                    'initial_shares': f"{initial_investment / current_price_calc:,.2f}",
                    'final_value_no_drip': f"${final_value_no_drip:,.2f}",
                    'final_shares_no_drip': f"{final_shares_no_drip:,.2f}",
                    'final_value_with_drip': f"${final_investment_value_with_drip:,.2f}",
                    'final_shares_with_drip': f"{final_shares_with_drip:,.2f}",
                    'yearly_breakdown_display': yearly_breakdown_for_display,
                    'historical_dividend_chart_data': historical_dividend_chart_data, # Raw data for Chart.js
                    'historical_price_chart_data': historical_price_chart_data,       # Raw data for Chart.js
                    'drip_portfolio_value_chart_data': portfolio_values_with_drip_chart, # Raw data for Chart.js
                    'drip_shares_owned_chart_data': shares_owned_with_drip_chart,     # Raw data for Chart.js
                    # Pass the user-selected payout_frequency for display
                    'payout_frequency_fetched': payout_frequency, 
                    'last_updated': drip_data.get('lastUpdated', 'N/A'), # Ensure last_updated is always present
                    'yield_on_cost': f"{yield_on_cost:,.2f}%",
                    'error': None
                })
                
                # Add data for comparison table
                comparison_data_for_table.append({
                    'Ticker': ticker_symbol,
                    'Initial Investment': f"${initial_investment:,.2f}",
                    'Initial Shares': f"{initial_investment / current_price_calc:,.2f}",
                    'Current Price': f"${current_price_calc:,.2f}",
                    'Annual Div per Share': f"${annual_dividend_per_share_start:,.2f}",
                    'Dividend Yield': f"{drip_data['dividendYield'] if drip_data['dividendYield'] is not None else dividend_yield:,.2f}%",
                    # Use the user-selected payout_frequency for the comparison table
                    'Payout Frequency': payout_frequency, 
                    '5-Yr Div Growth': f"{drip_data['annualDividendGrowthRate'] if drip_data['annualDividendGrowthRate'] is not None else annual_dividend_growth_rate:,.2f}%",
                    'Final Value (No DRIP)': f"${final_value_no_drip:,.2f}",
                    'Final Value (With DRIP)': f"${final_investment_value_with_drip:,.2f}"
                })
                
                time.sleep(0.1) # Small delay to avoid hammering yfinance API

            # Generate comparison table if more than one ticker and no major errors
            if len(selected_tickers) > 1 and not any(r.get('error') for r in results):
                if comparison_data_for_table:
                    comparison_data_df = pd.DataFrame(comparison_data_for_table)
                    comparison_table = comparison_data_df.to_html(classes='table table-striped table-bordered mt-3', index=False)
            
    return render_template('calculator.html', 
                            results=results, 
                            comparison_table=comparison_table,
                            stock_symbols=STOCK_SYMBOLS, # Pass all symbols for Select2 initial list
                            selected_tickers=selected_tickers, # Pass selected tickers to pre-fill Select2
                            initial_investment=initial_investment,
                            investment_years=investment_years,
                            initial_share_price=initial_share_price,
                            dividend_yield=dividend_yield,
                            annual_dividend_growth_rate=annual_dividend_growth_rate,
                            annual_stock_price_growth_rate=annual_stock_price_growth_rate,
                            payout_frequency=payout_frequency, # Pass payout_frequency back to template
                            drip_enabled=drip_enabled)


@app.route('/export_csv', methods=['POST'])
def export_csv():
    """Exports the detailed dividend calculation results to a CSV file."""
    tickers_str = request.form.get('export_tickers_hidden')
    initial_investment = request.form.get('export_initial_investment', type=float)
    investment_years = request.form.get('export_investment_years', type=int)
    drip_enabled = request.form.get('export_drip_enabled') == 'yes'
    initial_share_price_export = request.form.get('export_initial_share_price', type=float)
    dividend_yield_export = request.form.get('export_dividend_yield', type=float)
    annual_dividend_growth_rate_export = request.form.get('export_annual_dividend_growth_rate', type=float)
    annual_stock_price_growth_rate_export = request.form.get('export_annual_stock_price_growth_rate', type=float)
    payout_frequency_export = request.form.get('export_payout_frequency', 'Quarterly') # Retrieve for export

    if not tickers_str or not initial_investment or not investment_years:
        return "Missing data for export. Please ensure all required fields are present.", 400

    tickers = [t.strip().upper() for t in tickers_str.split(',') if t.strip()]
    all_data_for_export = []

    for ticker_symbol in tickers:
        drip_data = get_drip_stock_data(ticker_symbol)
        if "error" in drip_data:
            all_data_for_export.append({'Ticker': ticker_symbol, 'Error': drip_data['error']})
            continue

        # Use fetched data or export form data if fetched is None
        current_price_calc = drip_data['currentPrice'] if drip_data['currentPrice'] is not None else initial_share_price_export
        div_yield_decimal_calc = (drip_data['dividendYield'] / 100) if drip_data['dividendYield'] is not None else (dividend_yield_export / 100)
        div_growth_decimal_calc = (drip_data['annualDividendGrowthRate'] / 100) if drip_data['annualDividendGrowthRate'] is not None else (annual_dividend_growth_rate_export / 100)
        stock_price_growth_decimal_calc = (annual_stock_price_growth_rate_export / 100)
        # IMPORTANT: Use the payout_frequency_export from the form for calculations
        payout_frequency_sim = payout_frequency_export 
        
        annual_dividend_per_share_start = current_price_calc * div_yield_decimal_calc

        if annual_dividend_per_share_start == 0:
            all_data_for_export.append({'Ticker': ticker_symbol, 'Error': f"{ticker_symbol} does not pay dividends."})
            continue

        current_shares = initial_investment / current_price_calc
        current_stock_price = current_price_calc
        annual_dividend_per_share = annual_dividend_per_share_start
        
        yearly_breakdown = []
        for year in range(1, investment_years + 1):
            shares_at_start_of_year = current_shares
            stock_price_at_start_of_year = current_stock_price
            annual_dividend_per_share_at_start_of_year = annual_dividend_per_share

            dividends_received_this_year = 0
            reinvested_shares_this_year = 0

            number_of_payouts = {
                'Annual': 1,
                'Semi-Annual': 2,
                'Quarterly': 4,
                'Monthly': 12
            }.get(payout_frequency_sim, 1) # Use payout_frequency_sim here

            # Calculate period growth rates for export as well
            period_stock_price_growth_rate = (1 + stock_price_growth_decimal_calc) ** (1/number_of_payouts) - 1
            period_dividend_growth_rate = (1 + div_growth_decimal_calc) ** (1/number_of_payouts) - 1


            if drip_enabled:
                for i in range(number_of_payouts):
                    # Update stock price and dividend per share for the current period
                    current_stock_price_for_reinvestment = stock_price_at_start_of_year * ((1 + period_stock_price_growth_rate) ** i)
                    current_dividend_per_share_for_period = (annual_dividend_per_share_at_start_of_year / number_of_payouts) * ((1 + period_dividend_growth_rate) ** i)

                    dividends_received_this_period_period = shares_at_start_of_year * current_dividend_per_share_for_period
                    
                    shares_bought_this_period = 0
                    if current_stock_price_for_reinvestment > 0:
                        shares_bought_this_period = dividends_received_this_period_period / current_stock_price_for_reinvestment
                    
                    current_shares += shares_bought_this_period
                    dividends_received_this_year += dividends_received_this_period_period
                    reinvested_shares_this_year += shares_bought_this_period
            else:
                dividends_received_this_year = shares_at_start_of_year * annual_dividend_per_share_at_start_of_year
                reinvested_shares_this_year = 0

            # Update stock price and annual dividend per share for the *end* of the year
            current_stock_price = stock_price_at_start_of_year * (1 + stock_price_growth_decimal_calc)
            annual_dividend_per_share = annual_dividend_per_share_at_start_of_year * (1 + div_growth_decimal_calc)

            portfolio_value_end_of_year = current_shares * current_stock_price

            yearly_breakdown.append({
                'Year': year,
                'Ticker': ticker_symbol,
                'Shares Owned (Start)': shares_at_start_of_year,
                'Stock Price (Start)': stock_price_at_start_of_year,
                'Annual Dividend Per Share': annual_dividend_per_share_at_start_of_year,
                'Dividends Received (Year)': dividends_received_this_year,
                'Reinvested Shares (Year)': reinvested_shares_this_year,
                'Shares Owned (End)': current_shares,
                'Portfolio Value (End)': portfolio_value_end_of_year
            })
            
        all_data_for_export.extend(yearly_breakdown)
        time.sleep(0.1) # Small delay for API friendliness

    df = pd.DataFrame(all_data_for_export)
    csv_buffer = io.StringIO()
    # Format columns for CSV export to 2 decimal places for currency/shares
    df.to_csv(csv_buffer, index=False, float_format="%.2f")
    csv_buffer.seek(0)

    response = make_response(csv_buffer.getvalue())
    response.headers['Content-Disposition'] = 'attachment; filename=dividend_calculator_results.csv'
    response.headers['Content-Type'] = 'text/csv'
    return response

@app.route('/export_pdf', methods=['POST'])
def export_pdf():
    """Exports the detailed dividend calculation results to a PDF file."""
    try:
        # --- 1. Corrected Data Retrieval from Form (using _pdf suffixes) ---
        tickers_str = request.form.get('export_tickers_hidden_pdf')
        initial_investment = request.form.get('export_initial_investment_pdf', type=float)
        investment_years = request.form.get('export_investment_years_pdf', type=int)
        drip_enabled = request.form.get('export_drip_enabled_pdf') == 'yes'
        
        initial_share_price_export = request.form.get('export_initial_share_price_pdf', type=float)
        dividend_yield_export = request.form.get('export_dividend_yield_pdf', type=float)
        annual_dividend_growth_rate_export = request.form.get('export_annual_dividend_growth_rate_pdf', type=float)
        annual_stock_price_growth_rate_export = request.form.get('export_annual_dividend_growth_rate_pdf', type=float)
        payout_frequency_export = request.form.get('export_payout_frequency_pdf', 'Quarterly')

        if not tickers_str or initial_investment is None or investment_years is None:
            return "Missing data for export. Please ensure all required fields are present.", 400

        tickers = [t.strip().upper() for t in tickers_str.split(',') if t.strip()]
        
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(letter))
        styles = getSampleStyleSheet()
        story = []

        # --- Summary Information (consistent with previous PDF) ---
        story.append(Paragraph("Dividend Reinvestment Plan (DRIP) Calculator Results", styles['h1']))
        story.append(Spacer(1, 0.2 * 2.54 * 72)) 

        summary_text = f"<b>Initial Investment:</b> ${initial_investment:,.2f}<br/>" \
                       f"<b>Investment Duration:</b> {investment_years} Years<br/>" \
                       f"<b>DRIP Enabled:</b> {'Yes' if drip_enabled else 'No'}<br/>" \
                       f"<b>Default Initial Share Price (if API fails):</b> ${initial_share_price_export:,.2f}<br/>" \
                       f"<b>Default Dividend Yield (if API fails):</b> {dividend_yield_export:,.2f}%<br/>" \
                       f"<b>Default Annual Dividend Growth Rate (if API fails):</b> {annual_dividend_growth_rate_export:,.2f}%<br/>" \
                       f"<b>Default Annual Stock Price Growth Rate:</b> {annual_stock_price_growth_rate_export:,.2f}%<br/>" \
                       f"<b>Default Payout Frequency (if API fails):</b> {payout_frequency_export}"
        story.append(Paragraph(summary_text, styles['Normal']))
        story.append(Spacer(1, 0.2 * 2.54 * 72))

        # --- Loop through tickers, similar to CSV ---
        for ticker_symbol in tickers:
            drip_data = get_drip_stock_data(ticker_symbol)
            if "error" in drip_data:
                story.append(Paragraph(f"Error for {ticker_symbol}: {drip_data['error']}", styles['h3']))
                story.append(Spacer(1, 0.1 * 2.54 * 72))
                continue

            # Use fetched data or export form data if fetched is None
            current_price_calc = drip_data['currentPrice'] if drip_data.get('currentPrice') is not None else initial_share_price_export
            div_yield_decimal_calc = (drip_data['dividendYield'] / 100) if drip_data.get('dividendYield') is not None else (dividend_yield_export / 100)
            div_growth_decimal_calc = (drip_data['annualDividendGrowthRate'] / 100) if drip_data.get('annualDividendGrowthRate') is not None and drip_data['annualDividendGrowthRate'] is not None else (annual_dividend_growth_rate_export / 100)
            stock_price_growth_decimal_calc = (annual_stock_price_growth_rate_export / 100)
            # IMPORTANT: Use the payout_frequency_export from the form for calculations
            payout_frequency_sim = payout_frequency_export 
            
            annual_dividend_per_share_start = current_price_calc * div_yield_decimal_calc

            if annual_dividend_per_share_start == 0 and not drip_enabled:
                story.append(Paragraph(f"<b>{ticker_symbol}:</b> Does not pay dividends or dividend yield is zero.", styles['h3']))
                story.append(Spacer(1, 0.1 * 2.54 * 72))
            
            # --- Simulation Initialization ---
            current_shares = initial_investment / current_price_calc if current_price_calc > 0 else 0
            if current_shares == 0:
                story.append(Paragraph(f"<b>{ticker_symbol}:</b> Initial share price is zero or invalid, cannot calculate. Please check inputs.", styles['h3']))
                story.append(Spacer(1, 0.1 * 2.54 * 72))
                continue

            current_stock_price = current_price_calc
            annual_dividend_per_share = annual_dividend_per_share_start
            
            yearly_breakdown_data = []
            # Table Headers
            yearly_breakdown_data.append([
                'Year', 'Shares Owned (Start)', 'Stock Price (Start)', 
                'Annual Div Per Share', 'Dividends Received (Year)', 
                'Reinvested Shares (Year)', 'Shares Owned (End)', 'Portfolio Value (End)'
            ])

            # Initial state (Year 0) for PDF table - Added for clear starting point
            yearly_breakdown_data.append([
                '0',
                f"{initial_investment / initial_share_price_export if initial_share_price_export > 0 else 0:,.2f}",
                f"${initial_share_price_export:,.2f}",
                f"${current_price_calc * (dividend_yield_export / 100):,.2f}",
                "$0.00",
                "0.00",
                f"{initial_investment / initial_share_price_export if initial_share_price_export > 0 else 0:,.2f}",
                f"${initial_investment:,.2f}"
            ])

            # --- Simulation Loop (consistent with CSV logic) ---
            for year in range(1, investment_years + 1):
                shares_at_start_of_year = current_shares
                stock_price_at_start_of_year = current_stock_price
                annual_dividend_per_share_at_start_of_year = annual_dividend_per_share

                dividends_received_this_year = 0
                reinvested_shares_this_year = 0

                number_of_payouts = {
                    'Annual': 1,
                    'Semi-Annual': 2,
                    'Quarterly': 4,
                    'Monthly': 12
                }.get(payout_frequency_sim.capitalize(), 1)

                # Calculate period growth rates for PDF as well
                period_stock_price_growth_rate = (1 + stock_price_growth_decimal_calc) ** (1/number_of_payouts) - 1
                period_dividend_growth_rate = (1 + div_growth_decimal_calc) ** (1/number_of_payouts) - 1


                if drip_enabled:
                    for i in range(number_of_payouts):
                        # Update stock price and dividend per share for the current period
                        current_stock_price_for_reinvestment = current_stock_price * ((1 + period_stock_price_growth_rate) ** i)
                        current_dividend_per_share_for_period = (annual_dividend_per_share_at_start_of_year / number_of_payouts) * ((1 + period_dividend_growth_rate) ** i)

                        dividends_received_this_period = shares_at_start_of_year * current_dividend_per_share_for_period
                        
                        shares_bought_this_period = 0
                        if current_stock_price_for_reinvestment > 0:
                            shares_bought_this_period = dividends_received_this_period / current_stock_price_for_reinvestment
                        
                        current_shares += shares_bought_this_period
                        dividends_received_this_year += dividends_received_this_period
                        reinvested_shares_this_year += shares_bought_this_period
                else:
                    dividends_received_this_year = shares_at_start_of_year * annual_dividend_per_share_at_start_of_year
                    reinvested_shares_this_year = 0

                # Update stock price and annual dividend per share for the *end* of the year
                current_stock_price = current_stock_price * (1 + stock_price_growth_decimal_calc)
                annual_dividend_per_share = annual_dividend_per_share_at_start_of_year * (1 + div_growth_decimal_calc) # Corrected variable name

                portfolio_value_end_of_year = current_shares * current_stock_price

                yearly_breakdown_data.append([
                    str(year),
                    f"{shares_at_start_of_year:,.2f}",
                    f"${stock_price_at_start_of_year:,.2f}",
                    f"${annual_dividend_per_share_at_start_of_year:,.2f}",
                    f"${dividends_received_this_year:,.2f}",
                    f"{reinvested_shares_this_year:,.2f}",
                    f"{current_shares:,.2f}",
                    f"${portfolio_value_end_of_year:,.2f}"
                ])
                
            # No change needed here. The previous logic applied annual growth *after* the yearly breakdown data was appended.
            # To ensure the end-of-year values reflect the full annual growth for that year,
            # we need to ensure the `current_stock_price` and `annual_dividend_per_share`
            # used for `portfolio_value_end_of_year` calculation are the *final* values for that year.
            # The current structure already does this correctly by applying growth within the inner loop.


            # Add ticker-specific title and table
            story.append(Paragraph(f"Results for {ticker_symbol}", styles['h2']))
            story.append(Spacer(1, 0.1 * 2.54 * 72))

            # Add yearly breakdown table with colWidths for better layout
            num_columns = len(yearly_breakdown_data[0])
            page_width = landscape(letter)[0] - (doc.leftMargin + doc.rightMargin)
            col_widths = [page_width / num_columns] * num_columns

            table = Table(yearly_breakdown_data, colWidths=col_widths)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4a69bd')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                ('ALIGN', (0, 1), (0, -1), 'LEFT'),
                ('ALIGN', (1, 1), (-1, -1), 'RIGHT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f0f2f5')),
                ('GRID', (0, 0), (-1, -1), 1, colors.grey),
                ('LEFTPADDING', (0,0), (-1,-1), 4),
                ('RIGHTPADDING', (0,0), (-1,-1), 4),
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                ('FONTSIZE', (0,0), (-1,-1), 7),
            ]))
            story.append(table)
            story.append(Spacer(1, 0.5 * 2.54 * 72)) # Space after each ticker's table
            
            time.sleep(0.1) # Small delay for API friendliness, consistent with CSV

        doc.build(story)
        buffer.seek(0)
        
        return send_file(buffer, as_attachment=True, download_name='dividend_calculator_results.pdf', mimetype='application/pdf')

    except Exception as e:
        import traceback
        app.logger.error(f"Error in export_pdf: {e}\n{traceback.format_exc()}")
        return f"An error occurred while generating the PDF: {str(e)}. Please check the server logs for more details.", 500
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

