from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from playwright.sync_api import Page
import pandas as pd
from models.models import AccountClearingItem
from core.logger import get_logger
from core.prompt_service import ConsolePromptService, PromptService
from core.configuration import CONFIG
from loaders import BankVoucherLoader, FBL5NLoader, OnlineRefundsLoader, Fagll03Loader, Fagll03ClearedItemsLoader
from matchers import BankVoucherOnlineRefundMatcher, Fagll03OnlineRefundMatcher, Fagll03ClearedItemsMatcher
from sap import (
    SAPConnection,
    FBL5NReportGenerator,
    FAGLL03ReportGenerator,
    FAGLL03ClearedItemsReportGenerator,
    F53OutgoingPayment,
    FBRAReverseDocuments,
    F03CounterpartyClearing,
    F53Config,
    FBL5NConfig,
    FBRAConfig,
    F03Config,
    FAGLL03Config,
    REPORT_COLUMNS,
)
from sap.sap_exceptions import SAPRPAError, SAPDocumentLockedError
from collections.abc import Callable
from services import ExecutionSummary

logger = get_logger(__name__)



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Procesa devoluciones online en SAP.")
    parser.add_argument("--bank-voucher", type=Path, default=CONFIG.user.bank_voucher)
    parser.add_argument("--online-refunds", type=Path, default=CONFIG.user.online_refunds)
    parser.add_argument("--sap-url", default=CONFIG.sap.url)
    parser.add_argument("--report_date", default=CONFIG.report.report_date)
    parser.add_argument("--sap-user", default=CONFIG.user.sap_user)
    parser.add_argument("--sap-password", default=CONFIG.user.sap_password)
    parser.add_argument("--output-dir", type=Path, default=CONFIG.report.output_dir)
    parser.add_argument("--report-name", default=CONFIG.report.report_name)
    parser.add_argument("--company-code", default=CONFIG.sap.company_code)
    parser.add_argument("--customer-account", default=CONFIG.sap.customer_accounts_input)
    parser.add_argument("--layout", default=CONFIG.report.layout)
    return parser.parse_args()


def reverse_documents(
    sap,
    df_cleared: pd.DataFrame,
    process: Callable[[Page, str, str], str],
    prompt_service: PromptService,
) -> list[str]:
    """Anula cada documento de `df_cleared` invocando `process` (típicamente
    `FBRAReverseDocuments.process`).

    Manejo de errores, a propósito distinto según el tipo:

    - `SAPDocumentLockedError` (documento bloqueado por otro usuario en
      SAP): es la ÚNICA excepción que se intercepta aquí. Se le muestra al
      usuario el mensaje exacto que SAP mostró (capturado como innerText
      del diálogo de información — incluye qué usuario tiene el bloqueo) y
      se le pide decidir: reintentar o cancelar, con un límite de 5
      minutos. Si no responde a tiempo, se trata como cancelar. Si
      cancela (o no responde), se relanza el mismo error tal cual llegó
      (`raise` sin argumentos conserva el traceback original) para que lo
      capture el `except SAPDocumentLockedError` de `run()`.
    - Cualquier otro tipo de error NO se intercepta aquí: se propaga de
      inmediato, sin popup y sin reintento, porque no es de lo que este
      bloque se ocupa. Por eso `SAPDocumentLockedError` tiene que ser un
      tipo de excepción propio y distinguible del resto — es la señal que
      permite filtrar exactamente este caso de negocio (documento
      ocupado) de cualquier otro fallo técnico.
    """
    processed_docs: list[str] = []
    for _, row in df_cleared.iterrows():
        clearing_document = row[REPORT_COLUMNS.clearing_document]
        period = str(row[REPORT_COLUMNS.clearing_date].year)

        while True:
            page = sap.restart_page()
            try:
                process(page, clearing_document, period)
                processed_docs.append(clearing_document)
                break

            except SAPDocumentLockedError as error:
                logger.warning(
                    "Documento %s bloqueado, se solicita decisión al usuario | "
                    "mensaje SAP: %s",
                    clearing_document,
                    error.sap_message,
                )
                decision = prompt_service.ask_retry_cancel(
                    title="Documento bloqueado en SAP",
                    message=error.sap_message,
                    timeout=300,  # 5 minutos
                )

                if decision == "retry":
                    logger.info(
                        "Usuario eligió reintentar anulación de %s",
                        clearing_document,
                    )
                    continue

                logger.info(
                    "Usuario canceló (o no respondió a tiempo) para %s; "
                    "se propaga el error de documento bloqueado.",
                    clearing_document,
                )
                raise

    return processed_docs


def run(
    args: argparse.Namespace,
    prompt_service: PromptService | None = None,
) -> ExecutionSummary:
    # `prompt_service` desacopla a este orquestador de cómo se le pregunta
    # al usuario (GUI, consola, etc.). La GUI inyecta `GuiPromptService`
    # (gui/prompt_service.py); si se ejecuta por CLI sin GUI, se usa la
    # implementación de respaldo por consola.
    if prompt_service is None:
        prompt_service = ConsolePromptService()

    # La GUI entrega textos; el orquestador normaliza sus rutas para que el
    # mismo contrato funcione desde CLI, UI y futuras integraciones.
    args.bank_voucher = Path(args.bank_voucher)
    args.online_refunds = Path(args.online_refunds)
    args.output_dir = Path(args.output_dir)
    output_dir_files = Path(args.output_dir) / CONFIG.report.output_subdir
    output_dir_files.mkdir(parents=True, exist_ok=True)
    bank_voucher = BankVoucherLoader(args.bank_voucher)
    df_bank_voucher = bank_voucher.process()
    PAYMENT_DATE = bank_voucher.get_payment_date(df_bank_voucher)
    print("Fecha de pago obtenida")
    summary = ExecutionSummary(PAYMENT_DATE, args.bank_voucher, args.online_refunds)
    PAYMENT_TEXT = CONFIG.rules.payment_text_template.format(day=PAYMENT_DATE.day)
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
    f53_config = F53Config(company_code=args.company_code)
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
            docs_fbl5n = fbl5n.get_docs(df_fbl5n)

            docs_not_found = [
                    doc for doc in docs
                    if doc not in docs_fbl5n
                ]
            if docs_not_found:
                message = (
                    "Los siguientes documentos no aparecen en el reporte "
                    "descargado desde FBL5N: "
                    f"{docs_not_found}. "
                    "Es posible que estos documentos ya hayan sido compensados "
                    "o que no se encuentren actualmente como partidas abiertas."
                )
                raise Exception(message)
            
            TOTAL_AMOUNT_FBL5N = fbl5n.get_total_amount(df_fbl5n)
            DIFF_TOTAL_AMOUNT = abs(abs(TOTAL_AMOUNT_DEVON) - TOTAL_AMOUNT_FBL5N)

            if DIFF_TOTAL_AMOUNT > CONFIG.rules.tolerance:


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
                account_col=REPORT_COLUMNS.account,
                doc_col=REPORT_COLUMNS.document_number,
            )
            f53_message = F53OutgoingPayment(
                page, items, PAYMENT_DATE, PAYMENT_TEXT, TOTAL_AMOUNT_FBL5N,
                config=f53_config,
            ).process()
            summary.add_success("Pago saliente contabilizado (F-53)", f53_message)

        page = sap.restart_page()

        fagll03_config = FAGLL03Config(
            company_code=args.company_code,
            layout=CONFIG.report.layout,
        )
        f03_config = F03Config(company_code=args.company_code)
        fagll03 = FAGLL03ReportGenerator(
            page=page,
            company_code=args.company_code,
            accounts=CONFIG.sap.outgoing_bank_account,
            date=args.report_date,
            layout=CONFIG.report.layout,
            output_dir=output_dir_files,
            config=fagll03_config,
        )
        fagll03_path = fagll03.generate(CONFIG.report.fagll03_report_name)
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
            tolerance=fagll03_config.tolerance,
        )
        if df_payment.empty:
            raise ValueError(
                f"El pago no fue localizado en FAGLL03 para el valor "
                f"{-abs(target_amount)} y la fecha {PAYMENT_DATE.date()}."
            )
        df_payment = df_payment.assign(_merge="payment")
        df_to_post = pd.concat([df_match, df_payment], ignore_index=True)

        #asigno target_amount al valor verdadero que debe aparecer en SAP

        target_amount = df_payment[REPORT_COLUMNS.local_amount].iloc[0]
        PAYMENT_DOC = df_payment[REPORT_COLUMNS.document_number].iloc[0]

        pending = df_to_post[df_to_post["_merge"] == "left_only"].copy()
        ready_for_clearing = df_to_post[df_to_post["_merge"] == "both"][REPORT_COLUMNS.document_number].to_list()

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
                accounts=CONFIG.sap.outgoing_bank_account,
                date=args.report_date,
                output_dir=output_dir_files,
                config=fagll03_config,
            ).generate(CONFIG.report.cleared_items_report_name)

            if cleared_items_path:
                df_cleared = Fagll03ClearedItemsLoader(cleared_items_path).process()
                df_cleared = Fagll03ClearedItemsMatcher.match(
                    df_cleared,
                    pending,
                    id_column=CONFIG.inputs.online_customer_id,
                    value_column=CONFIG.inputs.online_amount,
                    tolerance=fagll03_config.tolerance,
                )

                if not df_cleared.empty:
                    # Se necesita eliminar los documentos de compensación duplicados
                    df_cleared_process = df_cleared.drop_duplicates(
                        REPORT_COLUMNS.clearing_document
                    ).copy()
                    fbra = FBRAReverseDocuments(page, FBRAConfig(company_code=args.company_code))
                    processed_docs = reverse_documents(
                        sap=sap,
                        df_cleared=df_cleared_process,
                        process=fbra.process,
                        prompt_service=prompt_service,
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
                                df_cleared[REPORT_COLUMNS.clearing_document].isin(processed_docs),
                                REPORT_COLUMNS.document_number,
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
            df_fagll03[REPORT_COLUMNS.document_number].isin(ready_for_clearing),
            REPORT_COLUMNS.local_amount,
        ].sum()

        raw_diff = amount_ready_for_clearing + target_amount
        diff_for_clearing = abs(raw_diff)
        has_difference = diff_for_clearing > CONFIG.rules.float_epsilon

        if diff_for_clearing > CONFIG.rules.tolerance:
            raise Exception(
                f"La diferencia para compensar es demasiado grande\n diferencia por {diff_for_clearing} "
                             
            )


        ready_for_clearing.append(PAYMENT_DOC)
        ready_for_clearing = list(set(ready_for_clearing))

        page = sap.restart_page()
        f03 = F03CounterpartyClearing(page, ready_for_clearing, f03_config)

        result = f03.clear( args.report_date)
        logger.info(result)
        summary.add_success("Compensación realizada (F-03)", result)

        logger.info("Proceso SAP finalizado | reporte=%s", report_path)

    except SAPDocumentLockedError as error:
        summary.add_error("Documento bloqueado durante anulación (FBRA)", error, error.document)
        logger.error("Proceso SAP detenido por documento bloqueado: %s", error)
    except (SAPRPAError, ValueError) as error:
        summary.add_error("Proceso SAP", error)
        logger.error("Proceso SAP no completado: %s", error)
    except Exception as error:
        summary.add_error("Error inesperado del proceso SAP", error)
        logger.exception("Proceso SAP no completado")
    finally:
        try:
            sap.close()
        finally:
            package_dir = summary.write_package(args.output_dir)
            logger.info("Resumen de ejecución guardado en %s", package_dir)

        # Limpieza best-effort: los reportes Excel descargados durante la
        # ejecución (FBL5N, FAGLL03, FAGLL03 compensadas) son solo insumos
        # de trabajo intermedios; una vez armado el paquete final en
        # `package_dir`, ya no se necesitan. Se intenta borrar toda la
        # carpeta "Reportes_SAP" para que en `output_dir` solo queden las
        # subcarpetas de cada ejecución (`compensacion_dev_online_...`).
        # Es "best-effort" (try/except) para no ocultar ni reemplazar el
        # resultado real del proceso si el borrado falla por cualquier
        # motivo (archivo en uso, permisos, etc.).
        try:
            if output_dir_files.exists():
                shutil.rmtree(output_dir_files)
                logger.info(
                    "Reportes Excel temporales eliminados: %s", output_dir_files
                )
        except Exception as cleanup_error:
            logger.warning(
                "No fue posible eliminar los reportes temporales en %s: %s",
                output_dir_files,
                cleanup_error,
            )
    return summary


if __name__ == "__main__":
    execution_summary = run(parse_args())
    print(execution_summary.render())