from __future__ import annotations

import argparse
from pathlib import Path
from playwright.sync_api import Page
import pandas as pd
from models.models import AccountClearingItem
from core.logger import get_logger
from loaders import BankVoucherLoader, FBL5NLoader, OnlineRefundsLoader, Fagll03Loader, Fagll03ClearedItemsLoader
from matchers import BankVoucherOnlineRefundMatcher, Fagll03OnlineRefundMatcher, Fagll03ClearedItemsMatcher
from sap import *
from sap.sap_exceptions import SAPRPAError
from sap.sap_exceptions import SAPDocumentLockedError
from collections.abc import Callable
from services import ExecutionSummary

logger = get_logger(__name__)

SAP_URL = "https://saps4h.gco.com.co/sap/bc/gui/sap/its/webgui/?sap-client=300&sap-language=ES#"
SAP_USER = ""
SAP_PASSWORD = ""
BANK_VOUCHER = Path()
ONLINE_REFUNDS = Path()
COMPANY_CODE = "1000"
CUSTOMER_ACCOUNT = "20001\n1000004288"
REPORT_DATE = pd.Timestamp.now()
TOLERANCE = 1_000
FLOAT_EPSILON = 0.01  # tolerancia solo para ruido de punto flotante, no de negocio
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
    parser.add_argument("--sap-password", default=SAP_PASSWORD)
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
                page.pause()
    return processed_docs


def run(args: argparse.Namespace) -> ExecutionSummary:
    # La GUI entrega textos; el orquestador normaliza sus rutas para que el
    # mismo contrato funcione desde CLI, UI y futuras integraciones.
    args.bank_voucher = Path(args.bank_voucher)
    args.online_refunds = Path(args.online_refunds)
    args.output_dir = Path(args.output_dir)
    output_dir_files = Path(args.output_dir) / "Reportes_SAP"
    output_dir_files.mkdir(parents=True, exist_ok=True)
    bank_voucher = BankVoucherLoader(args.bank_voucher)
    df_bank_voucher = bank_voucher.process()
    PAYMENT_DATE = bank_voucher.get_payment_date(df_bank_voucher)
    print("Fecha de pago obtenida")
    summary = ExecutionSummary(PAYMENT_DATE, args.bank_voucher, args.online_refunds)
    PAYMENT_TEXT = "Dev Online " + str(PAYMENT_DATE.day)
    df_online_refund = OnlineRefundsLoader(args.online_refunds).process()
    bank_voucher_matcher = BankVoucherOnlineRefundMatcher(df_bank_voucher, df_online_refund)
    df_online_refund = bank_voucher_matcher.match()
    docs = bank_voucher_matcher.split_docs(df_online_refund)
    TOTAL_AMOUNT_DEVON = bank_voucher_matcher.get_total_amount(df_online_refund)

    # `TOTAL_AMOUNT_FBL5N` solo se define si successful_payment es True más abajo;
    # se inicializa aquí para que target_amount nunca dependa de una variable
    # potencialmente no asignada.
    TOTAL_AMOUNT_FBL5N = None

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
        password=args.sap_password,
        config=fbl5n_config,
    )
    page = None
    try:
        page = sap.start()
        report_path = FBL5NReportGenerator(
            page=page,
            company_code=args.company_code,
            customer_account=args.customer_account,
            date=args.report_date,
            layout=args.layout,
            output_dir=output_dir_files,
            document_numbers_to_unlock=docs,
            config=fbl5n_config,
        ).generate(args.report_name)

        # Si se descargó el reporte, significa que el pago manual no existe;
        # si no, entonces este pago puede existir.
        successful_payment = bool(report_path)

        if successful_payment:
            page = sap.restart_page()

            fbl5n = FBL5NLoader(report_path)
            df_fbl5n = fbl5n.process()

            TOTAL_AMOUNT_FBL5N = fbl5n.get_total_amount(df_fbl5n)
            DIFF_TOTAL_AMOUNT = abs(abs(TOTAL_AMOUNT_DEVON) - TOTAL_AMOUNT_FBL5N)

            if DIFF_TOTAL_AMOUNT > TOLERANCE:
                docs_fbl5n = fbl5n.get_docs(df_fbl5n)

                docs_not_found = [
                    doc for doc in docs
                    if doc not in docs_fbl5n
                ]

                message = (
                    "La diferencia entre el archivo de devolución online y el "
                    "reporte descargado desde FBL5N es demasiado grande: "
                    f"diff {DIFF_TOTAL_AMOUNT}."
                )

                if docs_not_found:
                    message += (
                        "\nLos siguientes documentos no aparecen en el reporte "
                        "descargado desde FBL5N: "
                        f"{docs_not_found}. "
                        "Es posible que estos documentos ya hayan sido compensados "
                        "o que no se encuentren actualmente como partidas abiertas."
                    )

                raise Exception(message)

            items = AccountClearingItem.from_dataframe(
                df_fbl5n,
                account_col="Cuenta",
                doc_col="Nº documento",
            )
            f53_message = F53OutgoingPayment(
                page, items, PAYMENT_DATE, PAYMENT_TEXT, TOTAL_AMOUNT_FBL5N,
                config=f53_config,
            ).process()
            summary.add_success("Pago saliente contabilizado (F-53)", f53_message)

        page = sap.restart_page()

        fagll03 = FAGLL03ReportGenerator(
            page=page,
            company_code=args.company_code,
            accounts=OUTGOING_BANK_ACCOUNT,
            date=args.report_date,
            layout="/BOT COMPENS",
            output_dir=output_dir_files,
        )
        fagll03_path = fagll03.generate("fagll03")
        df_fagll03 = Fagll03Loader(fagll03_path).process(date=PAYMENT_DATE)

        df_match = Fagll03OnlineRefundMatcher.match(
            df_fagll03=df_fagll03,
            df_online_refund=df_online_refund,
        )
        target_amount = TOTAL_AMOUNT_FBL5N if successful_payment else TOTAL_AMOUNT_DEVON

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
        df_to_post = pd.concat([df_match, df_payment], ignore_index=True)

        #asigno target_amount al valor verdadero que debe aparecer en SAP

        target_amount = df_payment["Importe en moneda local"].iloc[0]
        PAYMENT_DOC = df_payment["Nº documento"].iloc[0]

        pending = df_to_post[df_to_post["_merge"] == "left_only"].copy()
        ready_for_clearing = df_to_post[df_to_post["_merge"] == "both"]["Nº documento"].to_list()

        # ------------------------------------------------------------------
        # RESOLUCIÓN DE PENDIENTES (si los hay): matching contra partidas ya
        # compensadas + anulación en FBRA. Todo lo que este bloque produce
        # (más partidas en ready_for_clearing, df_fagll03 refrescado) queda
        # aplicado antes de seguir; si no hay pendientes, se omite entero y
        # el flujo de abajo sigue con lo que ya había en ready_for_clearing.
        # ------------------------------------------------------------------
        if not pending.empty:
            page = sap.restart_page()
            cleared_items_path = FAGLL03ClearedItemsReportGenerator(
                page=page,
                company_code=args.company_code,
                accounts=OUTGOING_BANK_ACCOUNT,
                date=args.report_date,
                output_dir=output_dir_files,
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

                if not df_cleared.empty:
                    # Se necesita eliminar los documentos de compensación duplicados
                    df_cleared_process = df_cleared.drop_duplicates("Doc.compensación").copy()
                    fbra = FBRAReverseDocuments(page, FBRAConfig())
                    processed_docs = reverse_documents(
                        sap=sap, df_cleared=df_cleared_process, process=fbra.process
                    )
                    for document in processed_docs:
                        summary.add_success(
                            "Documento de compensación anulado (FBRA)",
                            f"La compensación {document} fue anulada correctamente.",
                            str(document),
                        )

                    # Se extiende la lista de partidas a compensar con las que
                    # quedaron libres tras la anulación en FBRA.
                    if processed_docs:
                        ready_for_clearing.extend(
                            df_cleared.loc[
                                df_cleared["Doc.compensación"].isin(processed_docs),
                                "Nº documento",
                            ].tolist()
                        )

                        fagll03.page = sap.restart_page()
                        fagll03_path = fagll03.generate("fagll03")
                        df_fagll03 = Fagll03Loader(fagll03_path).process(date=PAYMENT_DATE)

                # Si df_cleared quedó vacío, no había nada que anular en FBRA;
                # se sigue con ready_for_clearing tal como estaba, sin cortar el proceso.

        # ------------------------------------------------------------------
        # A partir de aquí SIEMPRE se ejecuta, haya habido pendientes o no,
        # porque ready_for_clearing y df_fagll03 ya reflejan el estado final.
        # ------------------------------------------------------------------
        amount_ready_for_clearing = df_fagll03.loc[
            df_fagll03["Nº documento"].isin(ready_for_clearing),
            "Importe en moneda local",
        ].sum()

        raw_diff = amount_ready_for_clearing + target_amount
        diff_for_clearing = abs(raw_diff)
        has_difference = diff_for_clearing > FLOAT_EPSILON

        if diff_for_clearing > TOLERANCE:
            raise Exception(
                f"La diferencia para compensar es demasiado grande\n diferencia por {diff_for_clearing} "
                             
            )

        # document_class solo indica qué clase de documento usar SI hay que
        # ajustar una diferencia; NO decide si se compensa o no —
        # F03CounterpartyClearing.clear() se llama siempre que haya partidas
        # en ready_for_clearing.
        document_class = None
        if has_difference:
            document_class = "50" if amount_ready_for_clearing > target_amount else "40"

        ready_for_clearing.append(PAYMENT_DOC)
        ready_for_clearing = list(set(ready_for_clearing))

        page = sap.restart_page()
        f03 = F03CounterpartyClearing(page, ready_for_clearing, F03Config())

        result = f03.clear(document_class, diff_for_clearing, REPORT_DATE)
        logger.info(result)
        summary.add_success("Compensación realizada (F-03)", result)

        logger.info("Proceso SAP finalizado | reporte=%s", report_path)

    except SAPDocumentLockedError as error:
        summary.add_error("Documento bloqueado durante anulación (FBRA)", error, error.document)
        logger.error("Proceso SAP detenido por documento bloqueado: %s", error)
    except (SAPRPAError, ValueError) as error:
        summary.add_error("Proceso SAP", error)
        if page is not None:
            page.pause()
        logger.error("Proceso SAP no completado: %s", error)
    except Exception as error:
        summary.add_error("Error inesperado del proceso SAP", error)
        logger.exception("Proceso SAP no completado")
    finally:
        sap.close()
        package_dir = summary.write_package(args.output_dir)
        logger.info("Resumen de ejecución guardado en %s", package_dir)
    return summary


if __name__ == "__main__":
    execution_summary = run(parse_args())
    print(execution_summary.render())
