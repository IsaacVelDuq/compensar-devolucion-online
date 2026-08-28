from __future__ import annotations

import argparse
from pathlib import Path
from playwright.sync_api import Page
import pandas as pd
from models.models import AccountClearingItem
from core.logger import get_logger
from loaders import BankVoucherLoader, FBL5NLoader, OnlineRefundsLoader, Fagll03Loader, Fagll03ClearedItemsLoader
from matchers import BankVoucherOnlineRefundMatcher, Fagll03OnlineRefundMatcher, Fagll03ClearedItemsMatcher
from models.models import AccountClearingItem
from sap import *
from sap.sap_exceptions import SAPRPAError
from collections.abc import Callable

logger = get_logger(__name__)

SAP_URL = "https://saps4h.gco.com.co/sap/bc/gui/sap/its/webgui/?sap-client=300&sap-language=ES#"
SAP_USER = "PRACPAGOS1"
SAP_PASSWORD = "prac222818Ss*"
BANK_VOUCHER = Path(r"C:\Users\isaacd\Downloads\Excel Online COM 25082026 $12240222.xls")
ONLINE_REFUNDS = Path(r"C:\Users\isaacd\Downloads\Devoluciones de dinero Comodin 24082026.xlsx")
COMPANY_CODE = "1000"
CUSTOMER_ACCOUNT = "20001\n1000004288"
REPORT_DATE = pd.Timestamp.now()
TOLERANCE = 1_000
LAYOUT = "/BOT_COMP_ON"
LAYOUT = "/BOT COMPENS"
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


def reverse_documents(
    sap,
    df_cleared: pd.DataFrame,
    process: Callable[[Page, str, str], bool],
):
    processed_docs = []
    for _, row in df_cleared.iterrows():
        clearing_document = row["Doc.compensación"]
        period = str(row["Fecha compensación"].year)

        while True:
            try:
                page = sap.restart_page()
                success = process(
                    page,
                    clearing_document,
                    period,
                )

                if success:
                    processed_docs.append(clearing_document)
                    break

            except Exception:
                raise
    return processed_docs
                

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


        
        #Si se descargo el reporte significa  el pago manual no exista, 
        #si no entonces este pago puede existir
        if report_path:
            successful_payment = True
        else:
            successful_payment = False
        
        if successful_payment:
            page = sap.restart_page()
            fbl5n = FBL5NLoader(report_path)
            df_fbl5n = fbl5n.process()
            TOTAL_AMOUNT_FBL5N = fbl5n.get_total_amount(df_fbl5n)
            DIFF_TOTAL_AMOUNT = abs(abs(TOTAL_AMOUNT_DEVON) - (TOTAL_AMOUNT_FBL5N))
            if DIFF_TOTAL_AMOUNT > TOLERANCE:
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

        fagll03 = FAGLL03ReportGenerator(page=page,company_code=args.company_code, accounts= OUTGOING_BANK_ACCOUNT,date=args.report_date,layout= "/BOT COMPENS", output_dir= args.output_dir)
        fagll03_path = fagll03.generate("fagll03")
        df_fagll03 = Fagll03Loader(fagll03_path).process(date= PAYMENT_DATE)

        df_match = Fagll03OnlineRefundMatcher.match(
            df_fagll03=df_fagll03,
            df_online_refund=df_online_refund,
        )
        target_amount = (
            TOTAL_AMOUNT_FBL5N
            if successful_payment
            else TOTAL_AMOUNT_DEVON
        )
        df_payment = Fagll03OnlineRefundMatcher.find_payment(
            df_fagll03=df_fagll03,
            amount=target_amount,
            payment_date=PAYMENT_DATE,
            tolerance=TOLERANCE,
        )
        if df_payment.empty:
            raise ValueError(
                f"El pago no fue localizado en FAGLL03 para el valor "
                f"{-abs(target_amount)} y la fecha {PAYMENT_DATE.date()}."
            )
        df_payment = df_payment.assign(_merge="payment")
        df_to_post = pd.concat(
            [df_match, df_payment],
            ignore_index=True,
        )

        page = sap.restart_page()

        pending = df_to_post[df_to_post["_merge"] == "left_only"].copy()
        ready_for_clearing = df_to_post[df_to_post["_merge"] == "both"].copy()
        ready_for_clearing = ready_for_clearing["Nº documento"].to_list()
        df_cleared = pd.DataFrame()

        if not pending.empty:
            page = sap.restart_page()
            cleared_items_path = FAGLL03ClearedItemsReportGenerator(
                page=page,
                company_code=args.company_code,
                accounts=OUTGOING_BANK_ACCOUNT,
                date=args.report_date,
                output_dir=args.output_dir,
                df_errores=df_errores,
            ).generate("fagll03_compensadas")

            if cleared_items_path:
                df_cleared = Fagll03ClearedItemsLoader(cleared_items_path).process()
                df_cleared = Fagll03ClearedItemsMatcher.match(
                    df_cleared,
                    pending,
                    id_column="Cédula cliente o nit",
                    value_column="Valor devolución o saldo",
                    tolerance=TOLERANCE,
                )

                if df_cleared.empty:
                    return

                df_cleared.to_excel(r"C:\Users\isaacd\Downloads\Compensadas.xlsx")
                #Se necesita eliminar los documentos de compensación duplicados
                df_cleared_process = df_cleared.drop_duplicates("Doc.compensación").copy()
                fbra = FBRAReverseDocuments(page,FBRAConfig)
                processed_docs = reverse_documents(sap=sap,df_cleared=df_cleared_process,process=fbra.process)
                # se extiende la lista de documentos a compensar con los que ya fueron anulados en fbra
                if processed_docs:
                    ready_for_clearing.extend(
                        df_cleared.loc[
                            df_cleared["Nº documento"].isin(processed_docs),
                            "Nº documento",
                        ].tolist()
                    )

                    fagll03.page = sap.restart_page()
                    fagll03_path = fagll03.generate("fagll03")
                    df_fagll03 = Fagll03Loader(fagll03_path).process(date= PAYMENT_DATE)



            amount_ready_for_clearing = df_fagll03.loc[
                df_fagll03["Nº documento"].isin(ready_for_clearing),
                "Importe en moneda local"
            ].sum()

            diff_for_clearing = abs(
                amount_ready_for_clearing - TOTAL_AMOUNT_FBL5N
            )

            if amount_ready_for_clearing > TOTAL_AMOUNT_FBL5N:
                greater_amount = "FAGLL03"
            elif TOTAL_AMOUNT_FBL5N > amount_ready_for_clearing:
                greater_amount = "FBL5N"
            else:
                greater_amount = "IGUALES"
        logger.info("Proceso SAP finalizado | reporte=%s", report_path)


    except (SAPRPAError, ValueError) as error:
        page.pause()
        logger.error("Proceso SAP no completado: %s", error)
    finally:
        sap.close()
    return df_errores


if __name__ == "__main__":
    errors = run(parse_args())
    if not errors.empty:
        print(errors.to_string(index=False))


