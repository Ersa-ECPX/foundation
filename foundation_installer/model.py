"""Text and small decisions for the installer, with no GTK in it so they can be tested anywhere."""

import os

from foundation.ui import human_size  # noqa: F401  (re-exported for the windows)

PRODUCT = "Ersa Compute Platform"

PARTITION_TITLES = {
    "ECP-ESP": "Boot",
    "ECP-RECOVERY": "Recovery",
    "ecp-root-a": "System slot A",
    "ecp-verity-a": "Integrity data for slot A",
    "ecp-root-b": "System slot B",
    "ecp-verity-b": "Integrity data for slot B",
    "ECP-DATA": "Your files and settings",
    "ECP-LIVE": "The installer's workspace",
}


def partition_title(label):
    if label in PARTITION_TITLES:
        return PARTITION_TITLES[label]
    return label


def disk_title(disk):
    return disk.get("model") or ("Removable disk" if disk.get("removable") else "Disk")


def disk_subtitle(disk):
    parts = [disk["path"], human_size(disk["size"])]
    if disk.get("removable"):
        parts.append("removable")
    if not disk.get("usable", True):
        parts.append(disk.get("reason") or "cannot be used")
    return " · ".join(parts)


def partition_size(partition):
    return human_size(partition["size"]) if partition["size"] else "The rest of the disk"


def erase_summary(plan):
    count = len(plan.get("will_erase", []))
    what = f"{plan['model']} ({plan['disk']}, {human_size(plan['size'])})" if plan.get("model") \
        else f"{plan['disk']} ({human_size(plan['size'])})"
    note = f" It currently holds {count} partition{'s' if count != 1 else ''}." if count else ""
    return f"Everything on {what} will be erased and cannot be recovered.{note}"


def failure_text(error):
    """One sentence for a failed call or job, from a ClientError or a JobResult."""
    name = getattr(error, "name", None) or getattr(error, "error_name", "")
    message = getattr(error, "message", None) or getattr(error, "error_message", "") or str(error)
    if name == "NotAuthorized":
        return "Permission was not granted to change the disk."
    if name == "Unreachable":
        return message
    if name == "Busy":
        return "Another installation is already running."
    return message or "The installation failed for an unknown reason."


def remaining_text(elapsed, fraction):
    """'About 3 minutes remaining', or '' until there is enough progress to estimate from."""
    if fraction < 0.05 or elapsed < 5 or fraction >= 1:
        return ""
    left = elapsed * (1 - fraction) / fraction
    if left < 60:
        return "Less than a minute remaining"
    minutes = round(left / 60)
    return f"About {minutes} minute{'s' if minutes != 1 else ''} remaining"


# Languages offered by the installer: (name in that language, locale). The installed system gets the locale
# in /etc/locale.conf; whether its translations are installed is up to the image.
LANGUAGES = (
    ("English (United States)", "en_US.UTF-8"), ("English (United Kingdom)", "en_GB.UTF-8"),
    ("Deutsch", "de_DE.UTF-8"), ("Fran\u00e7ais", "fr_FR.UTF-8"), ("Espa\u00f1ol", "es_ES.UTF-8"),
    ("Espa\u00f1ol (M\u00e9xico)", "es_MX.UTF-8"), ("Italiano", "it_IT.UTF-8"),
    ("Portugu\u00eas (Brasil)", "pt_BR.UTF-8"), ("Portugu\u00eas (Portugal)", "pt_PT.UTF-8"),
    ("Nederlands", "nl_NL.UTF-8"), ("Svenska", "sv_SE.UTF-8"), ("Norsk bokm\u00e5l", "nb_NO.UTF-8"),
    ("Dansk", "da_DK.UTF-8"), ("Suomi", "fi_FI.UTF-8"), ("Polski", "pl_PL.UTF-8"),
    ("\u010ce\u0161tina", "cs_CZ.UTF-8"), ("Magyar", "hu_HU.UTF-8"), ("Rom\u00e2n\u0103", "ro_RO.UTF-8"),
    ("\u0420\u0443\u0441\u0441\u043a\u0438\u0439", "ru_RU.UTF-8"),
    ("\u0423\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430", "uk_UA.UTF-8"),
    ("T\u00fcrk\u00e7e", "tr_TR.UTF-8"), ("\u0395\u03bb\u03bb\u03b7\u03bd\u03b9\u03ba\u03ac", "el_GR.UTF-8"),
    ("\u05e2\u05d1\u05e8\u05d9\u05ea", "he_IL.UTF-8"), ("\u0627\u0644\u0639\u0631\u0628\u064a\u0629", "ar_EG.UTF-8"),
    ("\u0939\u093f\u0928\u094d\u0926\u0940", "hi_IN.UTF-8"),
    ("\u09ac\u09be\u0982\u09b2\u09be", "bn_BD.UTF-8"), ("\u0e44\u0e17\u0e22", "th_TH.UTF-8"),
    ("Ti\u1ebfng Vi\u1ec7t", "vi_VN.UTF-8"), ("Bahasa Indonesia", "id_ID.UTF-8"),
    ("\u65e5\u672c\u8a9e", "ja_JP.UTF-8"), ("\ud55c\uad6d\uc5b4", "ko_KR.UTF-8"),
    ("\u4e2d\u6587 (\u7b80\u4f53)", "zh_CN.UTF-8"), ("\u4e2d\u6587 (\u7e41\u9ad4)", "zh_TW.UTF-8"),
    ("\u09ac\u09be\u0982\u09b2\u09be (\u09ad\u09be\u09b0\u09a4)", "bn_IN.UTF-8"), ("\u092e\u0930\u093e\u0920\u0940", "mr_IN.UTF-8"),
    ("\u0a97\u0ac1\u0a9c\u0ab0\u0abe\u0aa4\u0ac0", "gu_IN.UTF-8"), ("\u0a2a\u0a70\u0a1c\u0a3e\u0a2c\u0a40", "pa_IN.UTF-8"),
    ("\u0c95\u0ca8\u0ccd\u0ca8\u0ca1", "kn_IN.UTF-8"), ("\u0d2e\u0d32\u0d2f\u0d3e\u0d33\u0d02", "ml_IN.UTF-8"),
    ("\u0ba4\u0bae\u0bbf\u0bb4\u0bcd", "ta_IN.UTF-8"), ("\u0c24\u0c46\u0c32\u0c41\u0c17\u0c41", "te_IN.UTF-8"),
    ("\u0b13\u0b21\u0b3c\u0b3f\u0b06", "or_IN.UTF-8"), ("\u0985\u09b8\u09ae\u09c0\u09af\u09bc\u09be", "as_IN.UTF-8"),
    ("\u0627\u0631\u062f\u0648", "ur_PK.UTF-8"), ("\u0928\u0947\u092a\u093e\u0932\u0940", "ne_NP.UTF-8"),
    ("\u0938\u0902\u0938\u094d\u0915\u0943\u0924\u092e\u094d", "sa_IN.UTF-8"), ("\u0915\u094b\u0902\u0915\u0923\u0940", "kok_IN.UTF-8"),
    ("\u092e\u0948\u0925\u093f\u0932\u0940", "mai_IN.UTF-8"), ("\u0921\u094b\u0917\u0930\u0940", "doi_IN.UTF-8"),
    ("\u1c65\u1c5f\u1c71\u1c5b\u1c5f\u1c72\u1c64", "sat_IN.UTF-8"), ("\u0633\u0646\u068c\u064a", "sd_IN.UTF-8"),
    ("\u0641\u0627\u0631\u0633\u06cc", "fa_IR.UTF-8"), ("\u067e\u069a\u062a\u0648", "ps_AF.UTF-8"),
    ("\u0dc3\u0dd2\u0d82\u0dc4\u0dbd", "si_LK.UTF-8"), ("\u1019\u103c\u1014\u103a\u1019\u102c", "my_MM.UTF-8"),
    ("\u1781\u17d2\u1798\u17c2\u179a", "km_KH.UTF-8"), ("\u0ea5\u0eb2\u0ea7", "lo_LA.UTF-8"),
    ("\u12a0\u121b\u122d\u129b", "am_ET.UTF-8"), ("\u10e5\u10d0\u10e0\u10d7\u10e3\u10da\u10d8", "ka_GE.UTF-8"),
    ("\u0540\u0561\u0575\u0565\u0580\u0565\u0576", "hy_AM.UTF-8"),
)
DEFAULT_LANGUAGE = "en_US.UTF-8"


def language_name(locale):
    for name, code in LANGUAGES:
        if code == locale:
            return name
    return locale or "Default"


def timezones(zoneinfo_dir="/usr/share/zoneinfo"):
    """Every region/city zone the system knows, plus UTC, sorted. Read from the tz database on disk."""
    found = {"UTC"}
    for base, _dirs, files in os.walk(zoneinfo_dir):
        relative = os.path.relpath(base, zoneinfo_dir)
        if relative.split(os.sep)[0] in ("posix", "right", "Etc", ".") and relative != ".":
            continue
        for name in files:
            if relative == "." or name.startswith(".") or name[0].islower():
                continue
            found.add(f"{relative}/{name}")
    return sorted(found)


def current_timezone(localtime="/etc/localtime"):
    """The zone the running system uses (from the /etc/localtime link), or UTC."""
    try:
        target = os.readlink(localtime)
    except OSError:
        return "UTC"
    marker = "zoneinfo/"
    return target.split(marker, 1)[1] if marker in target else "UTC"


# "Welcome" in many languages, shown one after another on the welcome page.
GREETINGS = (
    "Welcome", "Willkommen", "Bienvenue", "Bienvenido", "Benvenuto", "Bem-vindo", "Welkom", "V\u00e4lkommen",
    "Velkommen", "Tervetuloa", "Witamy", "V\u00edtejte", "\u00dcdv\u00f6zl\u00fcnk",
    "\u0414\u043e\u0431\u0440\u043e \u043f\u043e\u0436\u0430\u043b\u043e\u0432\u0430\u0442\u044c",
    "\u039a\u03b1\u03bb\u03ce\u03c2 \u03aE\u03c1\u03b8\u03b1\u03c4\u03b5", "Ho\u015f geldiniz",
    "\u05d1\u05e8\u05d5\u05db\u05d9\u05dd \u05d4\u05d1\u05d0\u05d9\u05dd", "\u0645\u0631\u062d\u0628\u0627\u064b",
    "\u0938\u094d\u0935\u093e\u0917\u0924 \u0939\u0948", "\u0e22\u0e34\u0e19\u0e14\u0e35\u0e15\u0e49\u0e2d\u0e19\u0e23\u0e31\u0e1a",
    "Ch\u00e0o m\u1eebng", "Selamat datang", "\u3088\u3046\u3053\u305d", "\ud658\uc601\ud569\ub2c8\ub2e4",
    "\u6b22\u8fce", "\u6b61\u8fce",
    "\u09b8\u09cd\u09ac\u09be\u0997\u09a4\u09ae", "\u0bb5\u0bb0\u0bb5\u0bc7\u0bb1\u0bcd\u0b95\u0bbf\u0bb1\u0bcb\u0bae\u0bcd",
    "\u0c38\u0c4d\u0c35\u0c3e\u0c17\u0c24\u0c02", "\u0cb8\u0ccd\u0cb5\u0cbe\u0c97\u0ca4", "\u0d38\u0d4d\u0d35\u0d3e\u0d17\u0d24\u0d02",
    "\u0a97\u0ac1\u0a9c\u0ab0\u0abe\u0aa4\u0ac0".replace("\u0a97\u0ac1\u0a9c\u0ab0\u0abe\u0aa4\u0ac0", "\u0ab8\u0acd\u0ab5\u0abe\u0a97\u0aa4 \u0a9b\u0ac7"), "\u0a1c\u0a40 \u0a06\u0a07\u0a06\u0a02 \u0a28\u0a42\u0a70",
    "\u092e\u0930\u093e\u0920\u0940".replace("\u092e\u0930\u093e\u0920\u0940", "\u0938\u094d\u0935\u093e\u0917\u0924 \u0906\u0939\u0947"), "\u062e\u0648\u0634 \u0622\u0645\u062f\u06cc\u062f",
)
