import plistlib

from mailexport.accounts import discover_accounts


def test_discovery_from_plist(tmp_path):
    root = tmp_path / "V10"
    (root / "MailData").mkdir(parents=True)
    data = {
        "MailAccounts": [
            {"uniqueId": "AAA", "AccountName": "Work Gmail",
             "EmailAddresses": ["me@work.example"], "AccountType": "IMAPAccount"},
            {"uniqueId": "BBB", "AccountName": "Local", "AccountType": "LocalAccount"},
            {"uniqueId": "CCC", "AccountName": "Exchange",
             "Username": "me@corp.example", "AccountType": "EWSAccount"},
        ]
    }
    with open(root / "MailData" / "Accounts.plist", "wb") as f:
        plistlib.dump(data, f)
    accounts = discover_accounts(root, {})
    assert accounts["AAA"].email == "me@work.example"
    assert accounts["AAA"].display == "Work Gmail"
    assert "BBB" not in accounts  # local accounts skipped
    assert accounts["CCC"].email == "me@corp.example"


def test_inference_from_message_headers(tmp_path):
    import sys
    from pathlib import Path as P
    sys.path.insert(0, str(P(__file__).parent))
    from conftest import build_emlx, plain_message
    from mailexport.accounts import infer_account_from_messages

    acct = tmp_path / "UUID-1"
    messages = acct / "INBOX.mbox" / "X" / "Data" / "0" / "Messages"
    messages.mkdir(parents=True)
    for i, to in enumerate(["me@icloud.example", "me@icloud.example",
                            "list@other.example"]):
        msg = plain_message(to=to, message_id=f"<m{i}@x>")
        (messages / f"{i}.emlx").write_bytes(build_emlx(msg))
    inferred = infer_account_from_messages(acct)
    assert inferred is not None
    assert inferred.email == "me@icloud.example"
    assert inferred.discovered is False


def test_inference_empty_account(tmp_path):
    from mailexport.accounts import infer_account_from_messages
    acct = tmp_path / "UUID-2"
    acct.mkdir()
    assert infer_account_from_messages(acct) is None


def test_manual_mapping_overrides(tmp_path):
    root = tmp_path / "V10"
    root.mkdir()
    accounts = discover_accounts(root, {"DDD": "Personal iCloud <me@icloud.example>"})
    assert accounts["DDD"].email == "me@icloud.example"
    assert accounts["DDD"].display == "Personal iCloud"
    assert accounts["DDD"].discovered is False
