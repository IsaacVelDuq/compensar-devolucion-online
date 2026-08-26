from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from models.models import AccountClearingItem
from core.logger import get_logger
from loaders import BankVoucherLoader, FBL5NLoader, OnlineRefundsLoader, Fagll03Loader
from matchers import BankVoucherOnlineRefundMatcher, Fagll03DevolucionesMatcher
from models.models import AccountClearingItem
from sap import F53Config, FBL5NConfig, FBL5NReportGenerator, F53OutgoingPayment, SAPConnection, FAGLL03ReportGenerator
from sap.sap_exceptions import SAPRPAError


logger = get_logger(__name__)

SAP_URL = "https://saps4h.gco.com.co/sap/bc/gui/sap/its/webgui/?sap-client=300&sap-language=ES#"
SAP_USER = "PRACPAGOS1"
SAP_PASSWORD = "prac222818Ss*"
BANK_VOUCHER = Path(r"C:\Users\isaacd\Downloads\Excel Online COM 25082026 $12240222.xls")
ONLINE_REFUNDS = Path(r"C:\Users\isaacd\Downloads\Devoluciones de dinero Comodin 24082026.xlsx")
COMPANY_CODE = "1000"
CUSTOMER_ACCOUNT = "20001\n1000004288"
REPORT_DATE = pd.Timestamp.now()
LAYOUT = "/BOT_COMP_ON"
OUTPUT_DIR = Path("downloads")
REPORT_NAME = "fbl5n_report_TEMP"
DOCUMENT_TYPE = "KZ"
CURRENCY = "COP"
OUTGOING_BANK_ACCOUNT = "1110050302"
ACCOUNT_TYPE = "D"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Procesa devoluciones online en SAP.")
    parser.add_argument("--bank-voucher", type=Path, default=BANK_VOUCHER)
    parser.add_argument("--online-refunds", type=Path, default=ONLINE_REFUNDS)
    parser.add_argument("--sap-url", default=SAP_URL)
    parser.add_argument("--report_date", default=REPORT_DATE)
    parser.add_argument("--sap-user", default=SAP_USER)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--report-name", default=REPORT_NAME)
    parser.add_argument("--company-code", default=COMPANY_CODE)
    parser.add_argument("--customer-account", default=CUSTOMER_ACCOUNT)
    parser.add_argument("--layout", default=LAYOUT)
    return parser.parse_args()


def run(args: argparse.Namespace) -> pd.DataFrame:
    df_errores = pd.DataFrame()

    bank_voucher = BankVoucherLoader(args.bank_voucher)
    df_bank_voucher = bank_voucher.process()
    PAYMENT_DATE = bank_voucher.get_payment_date(df_bank_voucher)
    PAYMENT_TEXT = "Dev Online " + str(PAYMENT_DATE.day)
    df_online_refund = OnlineRefundsLoader(args.online_refunds).process()
    bank_voucher_matcher = BankVoucherOnlineRefundMatcher(df_bank_voucher, df_online_refund)
    df_online_refund = bank_voucher_matcher.match()
    docs = bank_voucher_matcher.split_docs(df_online_refund)
    TOTAL_AMOUNT_DEVON = bank_voucher_matcher.get_total_amount(df_online_refund)

    fbl5n_config = FBL5NConfig(company_code=args.company_code, layout=args.layout)
    f53_config = F53Config(
        company_code=args.company_code,
        document_type=DOCUMENT_TYPE,
        currency=CURRENCY,
        outgoing_bank_account=OUTGOING_BANK_ACCOUNT,
        account_type=ACCOUNT_TYPE,
    )
    sap = SAPConnection(
        url=args.sap_url,
        username=args.sap_user,
        password=SAP_PASSWORD,
        df_errores=df_errores,
        config=fbl5n_config,
    )
    try:
        page = sap.start()
        report_path = FBL5NReportGenerator(
            page=page,
            company_code=args.company_code,
            customer_account=args.customer_account,
            date=args.report_date,
            layout=args.layout,
            output_dir=args.output_dir,
            document_numbers_to_unlock=docs,
            config=fbl5n_config,
            df_errores=df_errores,
        ).generate(args.report_name)


        page = sap.restart_page()
        fbl5n = FBL5NLoader(report_path)
        df_fbl5n = fbl5n.process()
        TOTAL_AMOUNT_FBL5N = fbl5n.get_total_amount(df_fbl5n)
        DIFF_TOTAL_AMOUNT = abs(abs(TOTAL_AMOUNT_DEVON) - (TOTAL_AMOUNT_FBL5N))
        if DIFF_TOTAL_AMOUNT > 1_000:
            raise Exception(f"La diferencia entre el archivo de devolución online y el reporte descargado desde FBL5N " \
            f"es demasiado grande: diff {DIFF_TOTAL_AMOUNT}")
            
        items = AccountClearingItem.from_dataframe(
            df_fbl5n,
            account_col="Cuenta",
            doc_col="Nº documento",
        )
        F53OutgoingPayment(
            page, items, PAYMENT_DATE, PAYMENT_TEXT, TOTAL_AMOUNT_FBL5N,
            config=f53_config, df_errores=df_errores,
        ).process()

        page = sap.restart_page()


        fagll03_path = FAGLL03ReportGenerator(page=page,company_code=args.company_code, accounts= OUTGOING_BANK_ACCOUNT,date=args.report_date,layout= LAYOUT, output_dir= args.output_dir).generate("fagll03")
        df_fagll03 = Fagll03Loader(fagll03_path).process(date= PAYMENT_DATE)
        df_to_post = Fagll03DevolucionesMatcher().match(df_fagll03=df_fagll03,df_online_refund= df_online_refund,date = PAYMENT_DATE,amount=TOTAL_AMOUNT_FBL5N, text= PAYMENT_TEXT)
        df_to_post.to_excel(r"C:\Users\isaacd\Downloads\To_post.xlsx")
        logger.info("Proceso SAP finalizado | reporte=%s", report_path)

    except (SAPRPAError, ValueError) as error:
        logger.error("Proceso SAP no completado: %s", error)
    finally:
        sap.close()
    return df_errores


if __name__ == "__main__":
    errors = run(parse_args())
    if not errors.empty:
        print(errors.to_string(index=False))

