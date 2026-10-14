import os
from flask import Flask, redirect, url_for, render_template, session
from dotenv import load_dotenv

load_dotenv()

from routes.auth.auth import auth_bp
from routes.institutionSettings.institutionSettings import institution_settings_bp
from routes.members.members import members_bp
from routes.savingsAccounts.savingsAccounts import savings_bp
from routes.fixedAccounts.fixedAccounts import fixed_bp
from routes.loanProducts.loanProducts import loan_products_bp
from routes.loanApplications.loanApplications import loan_applications_bp
from routes.repayments.repayments import repayments_bp
from routes.loanPortfolio.loanPortfolio import loan_portfolio_bp
from routes.yearlySubscriptions.yearlySubscriptions import yearly_subscriptions_bp
from routes.sharesManagement.sharesManagement import shares_bp
from routes.accounts.accounts import accounts_bp
from routes.financialReports.financialReports import financial_reports_bp
from routes.dividendsManagement.dividendsManagement import dividends_bp
from routes.dashboard.dashboard import dashboard_bp
from routes.memberAuth.memberAuth import member_auth_bp
from routes.memberProfile.memberProfile import member_profile_bp
from routes.memberShares.memberShares import member_shares_bp
from routes.memberSubscriptions.memberSubscriptions import member_subscriptions_bp
from routes.memberLoans.memberLoans import member_loans_bp
from routes.memberAccounts.memberAccounts import member_accounts_bp
from routes.memberFixed.memberFixed import member_fixed_bp
from routes.memberDashboard.memberDashboard import member_dashboard_bp


def create_app():
    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")

    # Register blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(institution_settings_bp)
    app.register_blueprint(loan_applications_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(savings_bp)
    app.register_blueprint(fixed_bp)
    app.register_blueprint(loan_products_bp)
    app.register_blueprint(repayments_bp)
    app.register_blueprint(loan_portfolio_bp)
    app.register_blueprint(yearly_subscriptions_bp)
    app.register_blueprint(shares_bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(financial_reports_bp)
    app.register_blueprint(dividends_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(member_auth_bp)
    app.register_blueprint(member_profile_bp)
    app.register_blueprint(member_shares_bp)
    app.register_blueprint(member_subscriptions_bp)
    app.register_blueprint(member_loans_bp)
    app.register_blueprint(member_accounts_bp)
    app.register_blueprint(member_fixed_bp)
    app.register_blueprint(member_dashboard_bp)
    
    @app.route("/")
    def index():
        if session.get("user"):
            return redirect(url_for("auth.dashboard"))
        return redirect(url_for("auth.auth_page"))

    @app.errorhandler(404)
    def not_found(e):
        return render_template("auth/auth.html"), 404

    return app


app = create_app()

if __name__ == "__main__":
    # app.run(debug=os.getenv("FLASK_ENV") == "development", port=4000)
    from waitress import serve
    serve(app, host="0.0.0.0", port=40000)
