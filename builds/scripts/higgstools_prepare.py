"""Apply the reviewed HiggsTools 1.1.3 bridges before configuration.

The assignment-trace bridge is v2. The HS measurement-breakdown bridge is v1.
Patch bytes are pinned to the GMFit review digests and are applied with
patch --batch --fuzz=0 -p1. A later attempt skips a bridge that is already present.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ASSIGNMENT_SHA256 = "b05f09c3e4085baee0e331660bf7d01927f9a705b9cb25c0cec690f5d0e87b09"
BREAKDOWN_SHA256 = "930332a764e0e6364870bfb201a437843971bc8fe7019aa94692850c803f81c8"
ASSIGNMENT = base64.b64decode(
"""
    "LS0tIGEvcHl0aG9uL0hpZ2dzL0hpZ2dzU2lnbmFscy5jcHAKKysrIGIvcHl0aG9uL0hpZ2dzL0hp"
    "Z2dzU2lnbmFscy5jcHAKQEAgLTIsOCArMiwxNCBAQAogI2luY2x1ZGUgIkhpZ2dzL1ByZWRpY3Rp"
    "b25zLmhwcCIKICNpbmNsdWRlICJIaWdncy9TaWduYWxzLmhwcCIKICNpbmNsdWRlICJIaWdncy9z"
    "aWduYWxzL01lYXN1cmVtZW50LmhwcCIKKyNpbmNsdWRlICJwcmVkaWN0aW9ucy9DbHVzdGVyaW5n"
    "LmhwcCIKKyNpbmNsdWRlICJzaWduYWxzL01lYXN1cmVtZW50cy9SYXRlTWVhc3VyZW1lbnQuaHBw"
    "IgorI2luY2x1ZGUgInV0aWxpdGllcy9BbGdvcml0aG0uaHBwIgogI2luY2x1ZGUgPGZtdC9mb3Jt"
    "YXQuaD4KICNpbmNsdWRlIDxweWJpbmQxMS9zdGwuaD4KKyNpbmNsdWRlIDxzdGRleGNlcHQ+Cisj"
    "aW5jbHVkZSA8c3RyaW5nPgorI2luY2x1ZGUgPHZlY3Rvcj4KIAogdXNpbmcgbmFtZXNwYWNlIHB5"
    "OjpsaXRlcmFsczsKIApAQCAtMTIsMTAgKzE4LDExMSBAQAogbmFtZXNwYWNlIHsKIGNvbnN0ZXhw"
    "ciBNZWFzdXJlbWVudE9wdGlvbnMgZGVmYXVsdE9wdGlvbnN7fTsKIH0KKworbmFtZXNwYWNlIHsK"
    "Kworc3RkOjp2ZWN0b3I8c3RkOjpzdHJpbmc+Cithc3NpZ25lZFBhcnRpY2xlSWRzKGNvbnN0IHBy"
    "ZWRpY3Rpb25zOjpQYXJ0aWNsZVNldCAmY2x1c3RlcikgeworICAgIHN0ZDo6dmVjdG9yPHN0ZDo6"
    "c3RyaW5nPiBpZHM7CisgICAgaWRzLnJlc2VydmUoY2x1c3Rlci5zaXplKCkpOworICAgIGZvciAo"
    "Y29uc3QgYXV0byAmcGFydGljbGUgOiBjbHVzdGVyKSB7CisgICAgICAgIGlkcy5wdXNoX2JhY2so"
    "cGFydGljbGUuZ2V0KCkuaWQoKSk7CisgICAgfQorICAgIHJldHVybiBpZHM7Cit9CisKK2F1dG8g"
    "c2lnbmFsU3RyZW5ndGhGb3JUcmFjZShjb25zdCBDb21tb25NZWFzdXJlbWVudCAmbWVhc3VyZW1l"
    "bnQsCisgICAgICAgICAgICAgICAgICAgICAgICAgICAgY29uc3QgU3ViTWVhc3VyZW1lbnQ6Ok1v"
    "ZGlmaWNhdGlvbkZhY3RvcnMgJmZhY3RvcnMsCisgICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "UmVzY2FsZVRvUmVmTWFzcyByZXNjYWxpbmcpIHsKKyAgICByZXR1cm4gWyZtZWFzdXJlbWVudCwg"
    "JmZhY3RvcnMsIHJlc2NhbGluZ10oY29uc3QgcHJlZGljdGlvbnM6OlBhcnRpY2xlICZwKSB7Cisg"
    "ICAgICAgIGlmIChyZXNjYWxpbmcgPT0gUmVzY2FsZVRvUmVmTWFzczo6YWx3YXlzIHx8CisgICAg"
    "ICAgICAgICAocmVzY2FsaW5nID09IFJlc2NhbGVUb1JlZk1hc3M6OndpdGhpbk1hc3NVbmMgJiYK"
    "KyAgICAgICAgICAgICBzdGQ6OmFicyhtZWFzdXJlbWVudC5yZWZlcmVuY2VNYXNzKCkgLSBwLm1h"
    "c3MoKSkgPD0gcC5tYXNzVW5jKCkpKSB7CisgICAgICAgICAgICByZXR1cm4gbWVhc3VyZW1lbnQu"
    "c2lnbmFsU3RyZW5ndGgocCwgZmFjdG9ycywgTm9ybWFsaXplQXQ6OnBhcnRpY2xlTWFzcyk7Cisg"
    "ICAgICAgIH0KKyAgICAgICAgcmV0dXJuIG1lYXN1cmVtZW50LnNpZ25hbFN0cmVuZ3RoKHAsIGZh"
    "Y3RvcnMsIE5vcm1hbGl6ZUF0OjpyZWZlcmVuY2VNYXNzKTsKKyAgICB9OworfQorCitweTo6ZGlj"
    "dCBhc3NpZ25tZW50VHJhY2UoY29uc3QgU3ViTWVhc3VyZW1lbnQgJnN1Ym1lYXN1cmVtZW50LAor"
    "ICAgICAgICAgICAgICAgICAgICAgICAgIGNvbnN0IHByZWRpY3Rpb25zOjpQcmVkaWN0aW9ucyAm"
    "cHJlZGljdGlvbnMsCisgICAgICAgICAgICAgICAgICAgICAgICAgY29uc3QgU3ViTWVhc3VyZW1l"
    "bnQ6Ok1vZGlmaWNhdGlvbkZhY3RvcnMgJmZhY3RvcnMsCisgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgY29uc3QgTWVhc3VyZW1lbnRPcHRpb25zICZvcHRpb25zKSB7CisgICAgY29uc3QgYXV0byAq"
    "Y29tbW9uID0gZHluYW1pY19jYXN0PGNvbnN0IENvbW1vbk1lYXN1cmVtZW50ICo+KCZzdWJtZWFz"
    "dXJlbWVudCk7CisgICAgaWYgKGNvbW1vbiA9PSBudWxscHRyKSB7CisgICAgICAgIHRocm93IHN0"
    "ZDo6aW52YWxpZF9hcmd1bWVudCgiU3VibWVhc3VyZW1lbnQgZG9lcyBub3QgZXhwb3NlIGEgQ29t"
    "bW9uTWVhc3VyZW1lbnQgYXNzaWdubWVudCIpOworICAgIH0KKyAgICBjb25zdCBhdXRvIG5hdGl2"
    "ZSA9IHN1Ym1lYXN1cmVtZW50LmV2YWx1YXRlKHByZWRpY3Rpb25zLCBmYWN0b3JzLCBvcHRpb25z"
    "KTsKKyAgICBweTo6ZGljdCByZXN1bHQ7CisgICAgcmVzdWx0WyJuYXRpdmVfZXZhbHVhdGlvbiJd"
    "ID0gcHk6OmRpY3QoInJlc2lkdWFsIl9hID0gbmF0aXZlLnJlc2lkdWFsLAorICAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICJvYnNfdmFyaWFuY2UiX2EgPSBuYXRp"
    "dmUub2JzVmFyaWFuY2UsCisgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgInJlZl92YXJpYW5jZSJfYSA9IG5hdGl2ZS5yZWZWYXJpYW5jZSwKKyAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAiZXh0cmFfY2hpc3EiX2EgPSBuYXRp"
    "dmUuZXh0cmFDaGlzcSk7CisKKyAgICBpZiAoY29uc3QgYXV0byAqcmF0ZSA9IGR5bmFtaWNfY2Fz"
    "dDxjb25zdCBSYXRlTWVhc3VyZW1lbnQgKj4oJnN1Ym1lYXN1cmVtZW50KSkgeworICAgICAgICBj"
    "b25zdCBhdXRvIG11bHRpcGxpZXIgPSByYXRlLT5tYXNzU2Vuc2l0aXZlKCkKKyAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgID8gb3B0aW9ucy5tYXNzU2Vuc2l0aXZlQXNzaWdubWVu"
    "dFJhbmdlCisgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICA6IDEuOworICAgICAg"
    "ICBjb25zdCBhdXRvIGNsdXN0ZXIgPSBjb21tb24tPmFzc2lnblBhcnRpY2xlcygKKyAgICAgICAg"
    "ICAgIHByZWRpY3Rpb25zLCBvcHRpb25zLnRoZW9yeU1hc3NVbmNQREYsIG11bHRpcGxpZXIpOwor"
    "ICAgICAgICBjb25zdCBhdXRvIG11ID0gc2lnbmFsU3RyZW5ndGhGb3JUcmFjZSgqY29tbW9uLCBm"
    "YWN0b3JzLCBvcHRpb25zLnJlc2NhbGVUb1JlZk1hc3MpOworICAgICAgICBjb25zdCBhdXRvIHNp"
    "Z25hbFN0cmVuZ3RoID0gcHJlZGljdGlvbnM6OkNsdXN0ZXJpbmc6OmNvbWJpbmVkUmF0ZShtdSwg"
    "Y2x1c3Rlcik7CisgICAgICAgIHJlc3VsdFsib2JzZXJ2YWJsZV9raW5kIl0gPSAicmF0ZSI7Cisg"
    "ICAgICAgIHJlc3VsdFsiYXNzaWduZWRfcGFydGljbGVzIl0gPSBhc3NpZ25lZFBhcnRpY2xlSWRz"
    "KGNsdXN0ZXIpOworICAgICAgICByZXN1bHRbImFzc2lnbm1lbnRfbXVsdGlwbGllciJdID0gbXVs"
    "dGlwbGllcjsKKyAgICAgICAgcmVzdWx0WyJwcmVkaWN0ZWQiXSA9IHNpZ25hbFN0cmVuZ3RoICog"
    "cmF0ZS0+cmVmZXJlbmNlUmF0ZSgpOworICAgICAgICByZXN1bHRbIm11Il0gPSBzaWduYWxTdHJl"
    "bmd0aDsKKyAgICAgICAgcmVzdWx0WyJ1bml0Il0gPSAibmF0aXZlX3JhdGUiOworICAgICAgICBy"
    "ZXR1cm4gcmVzdWx0OworICAgIH0KKworICAgIGlmIChkeW5hbWljX2Nhc3Q8Y29uc3QgTWFzc01l"
    "YXN1cmVtZW50ICo+KCZzdWJtZWFzdXJlbWVudCkgIT0gbnVsbHB0cikgeworICAgICAgICBjb25z"
    "dCBhdXRvIG11bHRpcGxpZXIgPSBvcHRpb25zLm1hc3NTZW5zaXRpdmVBc3NpZ25tZW50UmFuZ2U7"
    "CisgICAgICAgIGNvbnN0IGF1dG8gY2x1c3RlciA9IGNvbW1vbi0+YXNzaWduUGFydGljbGVzKAor"
    "ICAgICAgICAgICAgcHJlZGljdGlvbnMsIG9wdGlvbnMudGhlb3J5TWFzc1VuY1BERiwgbXVsdGlw"
    "bGllcik7CisgICAgICAgIHJlc3VsdFsib2JzZXJ2YWJsZV9raW5kIl0gPSAibWFzcyI7CisgICAg"
    "ICAgIHJlc3VsdFsiYXNzaWduZWRfcGFydGljbGVzIl0gPSBhc3NpZ25lZFBhcnRpY2xlSWRzKGNs"
    "dXN0ZXIpOworICAgICAgICByZXN1bHRbImFzc2lnbm1lbnRfbXVsdGlwbGllciJdID0gbXVsdGlw"
    "bGllcjsKKyAgICAgICAgcmVzdWx0WyJ1bml0Il0gPSAiR2VWIjsKKyAgICAgICAgaWYgKCFjbHVz"
    "dGVyLmVtcHR5KCkpIHsKKyAgICAgICAgICAgIGNvbnN0IGF1dG8gbXUgPSBzaWduYWxTdHJlbmd0"
    "aEZvclRyYWNlKCpjb21tb24sIGZhY3RvcnMsIG9wdGlvbnMucmVzY2FsZVRvUmVmTWFzcyk7Cisg"
    "ICAgICAgICAgICBjb25zdCBhdXRvIFt3ZWlnaHRlZE1hc3NdID0KKyAgICAgICAgICAgICAgICBw"
    "cmVkaWN0aW9uczo6Q2x1c3RlcmluZzo6cmF0ZVdlaWdodGVkTWFzc2VzKG11LCBjbHVzdGVyKTsK"
    "KyAgICAgICAgICAgIHJlc3VsdFsicHJlZGljdGVkIl0gPSB3ZWlnaHRlZE1hc3MubWFzczsKKyAg"
    "ICAgICAgICAgIHJlc3VsdFsicHJlZGljdGlvbl91bmNlcnRhaW50eSJdID0gd2VpZ2h0ZWRNYXNz"
    "LnVuY2VydGFpbnR5OworICAgICAgICB9IGVsc2UgeworICAgICAgICAgICAgcmVzdWx0WyJwcmVk"
    "aWN0ZWQiXSA9IHB5Ojpub25lKCk7CisgICAgICAgICAgICByZXN1bHRbInByZWRpY3Rpb25fdW5j"
    "ZXJ0YWludHkiXSA9IHB5Ojpub25lKCk7CisgICAgICAgIH0KKyAgICAgICAgcmV0dXJuIHJlc3Vs"
    "dDsKKyAgICB9CisKKyAgICBpZiAoY29uc3QgYXV0byAqY291cGxpbmcgPSBkeW5hbWljX2Nhc3Q8"
    "Y29uc3QgQ291cGxpbmdNZWFzdXJlbWVudCAqPigmc3VibWVhc3VyZW1lbnQpKSB7CisgICAgICAg"
    "IGNvbnN0IGF1dG8gY2x1c3RlciA9IGNvdXBsaW5nLT5hc3NpZ25lZENvdXBsaW5nUGFydGljbGVz"
    "KHByZWRpY3Rpb25zLCBvcHRpb25zKTsKKyAgICAgICAgY29uc3QgYXV0byBraW5kID0gY291cGxp"
    "bmctPm1lYXN1cmVkQ291cGxpbmcoKTsKKyAgICAgICAgcmVzdWx0WyJvYnNlcnZhYmxlX2tpbmQi"
    "XSA9ICJjb3VwbGluZyI7CisgICAgICAgIHJlc3VsdFsiY291cGxpbmciXSA9IHN0ZDo6c3RyaW5n"
    "KG1hZ2ljX2VudW06OmVudW1fbmFtZShraW5kKSk7CisgICAgICAgIHJlc3VsdFsiYXNzaWduZWRf"
    "cGFydGljbGVzIl0gPSBhc3NpZ25lZFBhcnRpY2xlSWRzKGNsdXN0ZXIpOworICAgICAgICByZXN1"
    "bHRbImFzc2lnbm1lbnRfbXVsdGlwbGllciJdID0gMS47CisgICAgICAgIHJlc3VsdFsidW5pdCJd"
    "ID0ga2luZCA9PSBwcmVkaWN0aW9uczo6Q291cGxpbmc6OmFscGhhQ1BUYXVZdWsgPyAicmFkIiA6"
    "ICIxIjsKKyAgICAgICAgaWYgKCFjbHVzdGVyLmVtcHR5KCkpIHsKKyAgICAgICAgICAgIGNvbnN0"
    "IGF1dG8gbXUgPSBzaWduYWxTdHJlbmd0aEZvclRyYWNlKCpjb21tb24sIGZhY3RvcnMsIG9wdGlv"
    "bnMucmVzY2FsZVRvUmVmTWFzcyk7CisgICAgICAgICAgICByZXN1bHRbInByZWRpY3RlZCJdID0g"
    "dXRpbGl0aWVzOjp3ZWlnaHRlZE1lYW4oY2x1c3RlciwgbXUsCisgICAgICAgICAgICAgICAgW2tp"
    "bmRdKGNvbnN0IHByZWRpY3Rpb25zOjpQYXJ0aWNsZSAmcCkgeyByZXR1cm4gcC5jb3VwbGluZyhr"
    "aW5kKS52YWx1ZSgpOyB9KTsKKyAgICAgICAgfSBlbHNlIHsKKyAgICAgICAgICAgIHJlc3VsdFsi"
    "cHJlZGljdGVkIl0gPSBweTo6bm9uZSgpOworICAgICAgICB9CisgICAgICAgIHJldHVybiByZXN1"
    "bHQ7CisgICAgfQorICAgIHRocm93IHN0ZDo6aW52YWxpZF9hcmd1bWVudCgiVW5rbm93biBzdWJt"
    "ZWFzdXJlbWVudCB0eXBlIGluIGFzc2lnbm1lbnQgYnJpZGdlIik7Cit9CisKK30gLy8gbmFtZXNw"
    "YWNlCiAKIHZvaWQgYmluZEhpZ2dzU2lnbmFscyhweTo6bW9kdWxlICZoaWdncykgewogICAgIGF1"
    "dG8gaHMgPSBoaWdncy5kZWZfc3VibW9kdWxlKAogICAgICAgICAic2lnbmFscyIsICJUZXN0cyBh"
    "Z3JlZW1lbnQgd2l0aCBtZWFzdXJlbWVudHMgb2Ygc2NhbGFyIHJlc29uYW5jZXMuIik7CisgICAg"
    "aHMuYXR0cigiX2h0c0Fzc2lnbm1lbnRUcmFjZVZlcnNpb24iKSA9IDI7CiAKICAgICByZWdpc3Rl"
    "ckVudW08UERGPihocyk7CiAgICAgcmVnaXN0ZXJFbnVtPFJlc2NhbGVUb1JlZk1hc3M+KGhzKTsK"
    "QEAgLTg4LDYgKzE5NSwxMSBAQAogICAgICAgICAuZGVmX3JlYWR3cml0ZSgiaWdub3JlVGhlb3J5"
    "VW5jZXJ0YWludGllcyIsCiAgICAgICAgICAgICAgICAgICAgICAgICZNZWFzdXJlbWVudE9wdGlv"
    "bnM6Omlnbm9yZVRoZW9yeVVuY2VydGFpbnRpZXMpOwogCisgICAgaHMuZGVmKCJfaHRzQXNzaWdu"
    "bWVudFRyYWNlIiwgJmFzc2lnbm1lbnRUcmFjZSwKKyAgICAgICAgICAgInN1Ym1lYXN1cmVtZW50"
    "Il9hLCAicHJlZGljdGlvbnMiX2EsICJtb2RpZmljYXRpb25GYWN0b3JzIl9hLAorICAgICAgICAg"
    "ICAib3B0aW9ucyJfYSwKKyAgICAgICAgICAgIlJldHVybiB0aGUgbmF0aXZlIGFzc2lnbm1lbnQg"
    "YW5kIHJlc29sdmVkIHByZWRpY3Rpb24gZm9yIHJhdGUgb3IgbWFzcyBzdWJtZWFzdXJlbWVudHMu"
    "Iik7CisKICAgICBweTo6Y2xhc3NfPE1lYXN1cmVtZW50PihocywgIk1lYXN1cmVtZW50IikKICAg"
    "ICAgICAgLmRlZihweTo6aW5pdDxzdGQ6OnN0cmluZywgTWVhc3VyZW1lbnRPcHRpb25zPigpLCAi"
    "ZmlsZVBhdGgiX2EsCiAgICAgICAgICAgICAgIm9wdGlvbnMiX2EgPSBNZWFzdXJlbWVudE9wdGlv"
    "bnN7fSkKLS0tIGEvc3JjL3NpZ25hbHMvTWVhc3VyZW1lbnRzL1JhdGVNZWFzdXJlbWVudC5ocHAK"
    "KysrIGIvc3JjL3NpZ25hbHMvTWVhc3VyZW1lbnRzL1JhdGVNZWFzdXJlbWVudC5ocHAKQEAgLTE2"
    "OCw2ICsxNjgsMTMgQEAKIAogICAgIGRvdWJsZSBvYnNlcnZlZENvdXBsaW5nVW5jKFVuY2VydGFp"
    "bnR5IHVuY2VydGFpbnR5KSBjb25zdCBub2V4Y2VwdDsKIAorICAgIC8vIFNoYXJlZCBieSBldmFs"
    "dWF0ZSBhbmQgdGhlIHRyYWNlLCBpbmNsdWRpbmcgdGhlIGZpbmFsIGNvdXBsaW5nIGZpbHRlci4K"
    "KyAgICBwcmVkaWN0aW9uczo6UGFydGljbGVTZXQKKyAgICBhc3NpZ25lZENvdXBsaW5nUGFydGlj"
    "bGVzKGNvbnN0IHByZWRpY3Rpb25zOjpQcmVkaWN0aW9ucyAmcHJlZGljdGlvbnMsCisgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgIGNvbnN0IE1lYXN1cmVtZW50T3B0aW9ucyAmb3B0aW9ucykg"
    "Y29uc3Q7CisKKyAgICBwcmVkaWN0aW9uczo6Q291cGxpbmcgbWVhc3VyZWRDb3VwbGluZygpIGNv"
    "bnN0IG5vZXhjZXB0IHsgcmV0dXJuIGNvdXBfOyB9CisKICAgcHJpdmF0ZToKICAgICBwcmVkaWN0"
    "aW9uczo6Q291cGxpbmcgY291cF87CiAgICAgVW5jZXJ0YWluVmFsdWUgb2JzQ291cF87Ci0tLSBh"
    "L3NyYy9zaWduYWxzL01lYXN1cmVtZW50cy9SYXRlTWVhc3VyZW1lbnQuY3BwCisrKyBiL3NyYy9z"
    "aWduYWxzL01lYXN1cmVtZW50cy9SYXRlTWVhc3VyZW1lbnQuY3BwCkBAIC0yNzYsMTcgKzI3Niwy"
    "MyBAQAogICAgIHRocm93IHN0ZDo6cnVudGltZV9lcnJvcigidW5yZWFjaGFibGUiKTsgLy8gTENP"
    "Vl9FWENMX0xJTkUKIH0KIAorcHJlZGljdGlvbnM6OlBhcnRpY2xlU2V0IENvdXBsaW5nTWVhc3Vy"
    "ZW1lbnQ6OmFzc2lnbmVkQ291cGxpbmdQYXJ0aWNsZXMoCisgICAgY29uc3QgcHJlZGljdGlvbnM6"
    "OlByZWRpY3Rpb25zICZwcmVkaWN0aW9ucywKKyAgICBjb25zdCBNZWFzdXJlbWVudE9wdGlvbnMg"
    "Jm9wdGlvbnMpIGNvbnN0IHsKKyAgICBhdXRvIGNsdXN0ZXIgPSBhc3NpZ25QYXJ0aWNsZXMocHJl"
    "ZGljdGlvbnMsIG9wdGlvbnMudGhlb3J5TWFzc1VuY1BERiwgMS4pOworICAgIC8vIG9ubHkgY29u"
    "c2lkZXIgcGFydGljbGVzIGZvciB3aGljaCB0aGUgY291cGxpbmcgaGFzIGJlZW4gcHJvdmlkZWQK"
    "KyAgICB1dGlsaXRpZXM6OmVyYXNlRnJvbVNldElmKGNsdXN0ZXIsIFt0aGlzXShjb25zdCBwcmVk"
    "aWN0aW9uczo6UGFydGljbGUgJnApIHsKKyAgICAgICAgcmV0dXJuICFwLmNvdXBsaW5nKGNvdXBf"
    "KS5oYXNfdmFsdWUoKTsKKyAgICB9KTsKKyAgICByZXR1cm4gY2x1c3RlcjsKK30KKwogU3ViTWVh"
    "c3VyZW1lbnRFdmFsdWF0aW9uCiBDb3VwbGluZ01lYXN1cmVtZW50OjpldmFsdWF0ZShjb25zdCBw"
    "cmVkaWN0aW9uczo6UHJlZGljdGlvbnMgJnByZWRpY3Rpb25zLAogICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgICAgY29uc3QgTW9kaWZpY2F0aW9uRmFjdG9ycyAmbW9kRmFjcywKICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgIGNvbnN0IE1lYXN1cmVtZW50T3B0aW9ucyAmb3B0aW9ucykg"
    "Y29uc3QgewogICAgIHN0YXRpYyBjb25zdCBhdXRvIGxvZyA9IGxvZ2dlcigpOwotCi0gICAgYXV0"
    "byBjbHVzdGVyID0gYXNzaWduUGFydGljbGVzKHByZWRpY3Rpb25zLCBvcHRpb25zLnRoZW9yeU1h"
    "c3NVbmNQREYsIDEuKTsKLSAgICAvLyBvbmx5IGNvbnNpZGVyIHBhcnRpY2xlcyBmb3Igd2hpY2gg"
    "dGhlIGNvdXBsaW5nIGhhcyBiZWVuIHByb3ZpZGVkCi0gICAgdXRpbGl0aWVzOjplcmFzZUZyb21T"
    "ZXRJZihjbHVzdGVyLCBbdGhpc10oY29uc3QgcHJlZGljdGlvbnM6OlBhcnRpY2xlICZwKSB7Ci0g"
    "ICAgICAgIHJldHVybiAhcC5jb3VwbGluZyhjb3VwXykuaGFzX3ZhbHVlKCk7Ci0gICAgfSk7Cisg"
    "ICAgY29uc3QgYXV0byBjbHVzdGVyID0gYXNzaWduZWRDb3VwbGluZ1BhcnRpY2xlcyhwcmVkaWN0"
    "aW9ucywgb3B0aW9ucyk7CiAgICAgaWYgKGNsdXN0ZXIuZW1wdHkoKSkgewogICAgICAgICBsb2ct"
    "PnRyYWNlKCJObyBwYXJ0aWNsZXMgYXNzaWduZWQgdG8gY291cGxpbmcgbWVhc3VyZW1lbnQgeWll"
    "bGRpbmcgYSAiCiAgICAgICAgICAgICAgICAgICAgImNoaXNxIHBlbmFsdHkgb2Yge30iLAo="
""")
BREAKDOWN = base64.b64decode(
"""
    "LS0tIGEvaW5jbHVkZS9IaWdncy9TaWduYWxzLmhwcAorKysgYi9pbmNsdWRlL0hpZ2dzL1NpZ25h"
    "bHMuaHBwCkBAIC0xNyw2ICsxNyw3IEBACiAjaW5jbHVkZSA8bWVtb3J5PgogI2luY2x1ZGUgPHN0"
    "cmluZz4KICNpbmNsdWRlIDx1bm9yZGVyZWRfbWFwPgorI2luY2x1ZGUgPHV0aWxpdHk+CiAjaW5j"
    "bHVkZSA8dmVjdG9yPgogCiBuYW1lc3BhY2UgSGlnZ3MgewpAQCAtNzIsNiArNzMsMTMgQEAKICAg"
    "ICBvcGVyYXRvcigpKGNvbnN0IEhpZ2dzOjpwcmVkaWN0aW9uczo6UHJlZGljdGlvbnMgJnByZWRp"
    "Y3Rpb25zLAogICAgICAgICAgICAgICAgY29uc3QgTW9kaWZpY2F0aW9uRmFjdG9ycyAmbW9kaWZp"
    "Y2F0aW9uRmFjdG9ycyA9IHt9KSBjb25zdDsKIAorICAgIHVzaW5nIE1lYXN1cmVtZW50QnJlYWtk"
    "b3duID0gc3RkOjp1bm9yZGVyZWRfbWFwPHN0ZDo6c2l6ZV90LCBkb3VibGU+OworICAgIC8vISBS"
    "ZXR1cm4gdGhlIHVuY2hhbmdlZCBnbG9iYWwgY2hpLXNxdWFyZSBhbmQgaXRzIG1lYXN1cmVtZW50"
    "LWJsb2NrIHRlcm1zLgorICAgIHN0ZDo6cGFpcjxkb3VibGUsIE1lYXN1cmVtZW50QnJlYWtkb3du"
    "PgorICAgIGNoaXNxV2l0aE1lYXN1cmVtZW50QnJlYWtkb3duKAorICAgICAgICBjb25zdCBIaWdn"
    "czo6cHJlZGljdGlvbnM6OlByZWRpY3Rpb25zICZwcmVkaWN0aW9ucywKKyAgICAgICAgY29uc3Qg"
    "TW9kaWZpY2F0aW9uRmFjdG9ycyAmbW9kaWZpY2F0aW9uRmFjdG9ycyA9IHt9KSBjb25zdDsKKwog"
    "ICAgIC8vISBUaGUgdG90YWwgbnVtYmVyIG9mIG9ic2VydmFibGVzIGluIHRoZSBsb2FkZWQgbWVh"
    "c3VyZW1lbnRzCiAgICAgc3RkOjpzaXplX3Qgb2JzZXJ2YWJsZUNvdW50KCkgY29uc3Qgbm9leGNl"
    "cHQ7CiAKQEAgLTc5LDYgKzg3LDkgQEAKICAgICBjb25zdCBzdGQ6OnZlY3RvcjxNZWFzdXJlbWVu"
    "dD4gJm1lYXN1cmVtZW50cygpIGNvbnN0IG5vZXhjZXB0OwogCiAgIHByaXZhdGU6CisgICAgZG91"
    "YmxlIGV2YWx1YXRlKGNvbnN0IEhpZ2dzOjpwcmVkaWN0aW9uczo6UHJlZGljdGlvbnMgJnByZWRp"
    "Y3Rpb25zLAorICAgICAgICAgICAgICAgICAgICBjb25zdCBNb2RpZmljYXRpb25GYWN0b3JzICZt"
    "b2RpZmljYXRpb25GYWN0b3JzLAorICAgICAgICAgICAgICAgICAgICBNZWFzdXJlbWVudEJyZWFr"
    "ZG93biAqYnJlYWtkb3duKSBjb25zdDsKICAgICBzdGQ6OnZlY3RvcjxNZWFzdXJlbWVudD4gbWVh"
    "c3VyZW1lbnRzXzsKICAgICBzdGQ6OnNpemVfdCBuU3ViTWVhc3VyZW1lbnRzXzsKICAgICBzdGQ6"
    "OnNoYXJlZF9wdHI8R2xvYmFsQ29ycmVsYXRpb25zPiBjb3JyXzsKLS0tIGEvc3JjL3NpZ25hbHMv"
    "U2lnbmFscy5jcHAKKysrIGIvc3JjL3NpZ25hbHMvU2lnbmFscy5jcHAKQEAgLTg4LDkgKzg4LDIz"
    "IEBACiAKIGRvdWJsZSBTaWduYWxzOjpvcGVyYXRvcigpKAogICAgIGNvbnN0IFByZWRpY3Rpb25z"
    "ICZwcmVkaWN0aW9ucywKLSAgICBjb25zdCBzdGQ6OnVub3JkZXJlZF9tYXA8Ci0gICAgICAgIHN0"
    "ZDo6c2l6ZV90LCBzdGQ6OnVub3JkZXJlZF9tYXA8c3RkOjpzdHJpbmcsIHN0ZDo6dmVjdG9yPGRv"
    "dWJsZT4+PgotICAgICAgICAmbW9kaWZpY2F0aW9uRmFjdG9ycykgY29uc3QgeworICAgIGNvbnN0"
    "IE1vZGlmaWNhdGlvbkZhY3RvcnMgJm1vZGlmaWNhdGlvbkZhY3RvcnMpIGNvbnN0IHsKKyAgICBy"
    "ZXR1cm4gZXZhbHVhdGUocHJlZGljdGlvbnMsIG1vZGlmaWNhdGlvbkZhY3RvcnMsIG51bGxwdHIp"
    "OworfQorCitzdGQ6OnBhaXI8ZG91YmxlLCBTaWduYWxzOjpNZWFzdXJlbWVudEJyZWFrZG93bj4K"
    "K1NpZ25hbHM6OmNoaXNxV2l0aE1lYXN1cmVtZW50QnJlYWtkb3duKAorICAgIGNvbnN0IFByZWRp"
    "Y3Rpb25zICZwcmVkaWN0aW9ucywKKyAgICBjb25zdCBNb2RpZmljYXRpb25GYWN0b3JzICZtb2Rp"
    "ZmljYXRpb25GYWN0b3JzKSBjb25zdCB7CisgICAgTWVhc3VyZW1lbnRCcmVha2Rvd24gYnJlYWtk"
    "b3duOworICAgIGNvbnN0IGF1dG8gdG90YWwgPSBldmFsdWF0ZShwcmVkaWN0aW9ucywgbW9kaWZp"
    "Y2F0aW9uRmFjdG9ycywgJmJyZWFrZG93bik7CisgICAgcmV0dXJuIHt0b3RhbCwgc3RkOjptb3Zl"
    "KGJyZWFrZG93bil9OworfQorCitkb3VibGUgU2lnbmFsczo6ZXZhbHVhdGUoCisgICAgY29uc3Qg"
    "UHJlZGljdGlvbnMgJnByZWRpY3Rpb25zLAorICAgIGNvbnN0IE1vZGlmaWNhdGlvbkZhY3RvcnMg"
    "Jm1vZGlmaWNhdGlvbkZhY3RvcnMsCisgICAgTWVhc3VyZW1lbnRCcmVha2Rvd24gKmJyZWFrZG93"
    "bikgY29uc3QgewogICAgIGF1dG8gcmVzaWR1YWxzID0gRWlnZW46OlZlY3RvclhkKG5TdWJNZWFz"
    "dXJlbWVudHNfKTsKICAgICBhdXRvIG9ic2VydmVkVmFyaWFuY2VzID0gRWlnZW46OlZlY3Rvclhk"
    "KG5TdWJNZWFzdXJlbWVudHNfKTsKICAgICBhdXRvIGV4cGVjdGVkVmFyaWFuY2VzID0gRWlnZW46"
    "OlZlY3RvclhkKG5TdWJNZWFzdXJlbWVudHNfKTsKQEAgLTEwNSwxOCArMTE5LDMzIEBACiAgICAg"
    "ICAgIGF1dG8gcmVmUmF0ZVZhckJsb2NrID0KICAgICAgICAgICAgIGV4cGVjdGVkVmFyaWFuY2Vz"
    "LnNlZ21lbnQoc3ViTWVhc0NvdW50ZXIsIG5TdWJNZWFzKTsKIAotICAgICAgICBleHRyYUNoaXNx"
    "ICs9IGV2YWx1YXRlU3ViTWVhc3VyZW1lbnRzKAorICAgICAgICBjb25zdCBhdXRvIGV4dHJhID0g"
    "ZXZhbHVhdGVTdWJNZWFzdXJlbWVudHMoCiAgICAgICAgICAgICBwcmVkaWN0aW9ucywgbWVhc3Vy"
    "ZW1lbnQsCiAgICAgICAgICAgICB1dGlsaXRpZXM6OmdldFdpdGhEZWZhdWx0KG1vZGlmaWNhdGlv"
    "bkZhY3RvcnMsIG1lYXN1cmVtZW50LmlkKCksCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgIE1lYXN1cmVtZW50OjpNb2RpZmljYXRpb25GYWN0b3Jze30pLAogICAgICAgICAg"
    "ICAgcmVzQmxvY2ssIG1vZGVsUmF0ZVZhckJsb2NrLCByZWZSYXRlVmFyQmxvY2spOworICAgICAg"
    "ICBleHRyYUNoaXNxICs9IGV4dHJhOworICAgICAgICBpZiAoYnJlYWtkb3duKSB7CisgICAgICAg"
    "ICAgICBicmVha2Rvd24tPmVtcGxhY2UobWVhc3VyZW1lbnQuaWQoKSwgZXh0cmEpOworICAgICAg"
    "ICB9CiAgICAgICAgIHN1Yk1lYXNDb3VudGVyICs9IG5TdWJNZWFzOwogICAgIH0KIAogICAgIGF1"
    "dG8gY292YXJpYW5jZSA9IGNvbXB1dGVDb3ZhcmlhbmNlKGNvcnJfLT5leHBlcmltZW50YWwsIG9i"
    "c2VydmVkVmFyaWFuY2VzLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "IGNvcnJfLT50aGVvcnksIGV4cGVjdGVkVmFyaWFuY2VzKTsKIAotICAgIHJldHVybiBjb21wdXRl"
    "Q2hpc3EocmVzaWR1YWxzLCBjb3ZhcmlhbmNlKSArIGV4dHJhQ2hpc3E7CisgICAgaWYgKCFicmVh"
    "a2Rvd24pIHsKKyAgICAgICAgcmV0dXJuIGNvbXB1dGVDaGlzcShyZXNpZHVhbHMsIGNvdmFyaWFu"
    "Y2UpICsgZXh0cmFDaGlzcTsKKyAgICB9CisgICAgY29uc3QgYXV0byB0ZXJtcyA9IGNvbXB1dGVD"
    "aGlzcUNvbnRyaWJ1dGlvbnMocmVzaWR1YWxzLCBjb3ZhcmlhbmNlKTsKKyAgICBzdWJNZWFzQ291"
    "bnRlciA9IDA7CisgICAgZm9yIChjb25zdCBhdXRvICZtZWFzdXJlbWVudCA6IG1lYXN1cmVtZW50"
    "c18pIHsKKyAgICAgICAgY29uc3QgYXV0byBuU3ViTWVhcyA9IG1lYXN1cmVtZW50Lm5TdWJNZWFz"
    "dXJlbWVudHMoKTsKKyAgICAgICAgYnJlYWtkb3duLT5hdChtZWFzdXJlbWVudC5pZCgpKSArPQor"
    "ICAgICAgICAgICAgdGVybXMuc2VnbWVudChzdWJNZWFzQ291bnRlciwgblN1Yk1lYXMpLnN1bSgp"
    "OworICAgICAgICBzdWJNZWFzQ291bnRlciArPSBuU3ViTWVhczsKKyAgICB9CisgICAgcmV0dXJu"
    "IHRlcm1zLnN1bSgpICsgZXh0cmFDaGlzcTsKIH0KIAogc3RkOjpzaXplX3QgU2lnbmFsczo6b2Jz"
    "ZXJ2YWJsZUNvdW50KCkgY29uc3Qgbm9leGNlcHQgewotLS0gYS9weXRob24vSGlnZ3MvSGlnZ3NT"
    "aWduYWxzLmNwcAorKysgYi9weXRob24vSGlnZ3MvSGlnZ3NTaWduYWxzLmNwcApAQCAtMTE1LDEy"
    "ICsxMTUsMTYgQEAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBzZWxmLnJlZmVyZW5j"
    "ZSgpKTsKICAgICAgICAgfSk7CiAKKyAgICBocy5hdHRyKCJfaHRzTWVhc3VyZW1lbnRCcmVha2Rv"
    "d25WZXJzaW9uIikgPSAxOwogICAgIHB5OjpjbGFzc188U2lnbmFscz4oaHMsICJTaWduYWxzIikK"
    "ICAgICAgICAgLmRlZihweTo6aW5pdDxzdGQ6OnN0cmluZywgTWVhc3VyZW1lbnRPcHRpb25zPigp"
    "LCAiZGF0YVBhdGgiX2EsCiAgICAgICAgICAgICAgIm1lYXN1cmVtZW50T3B0aW9ucyJfYSA9IE1l"
    "YXN1cmVtZW50T3B0aW9uc3t9KQogICAgICAgICAuZGVmKCJfX2NhbGxfXyIsICZTaWduYWxzOjpv"
    "cGVyYXRvcigpLCAicHJlZGljdGlvbnMiX2EsCiAgICAgICAgICAgICAgIm1vZGlmaWNhdGlvbkZh"
    "Y3RvcnMiX2EgPSBTaWduYWxzOjpNb2RpZmljYXRpb25GYWN0b3Jze30pCiAgICAgICAgIC5kZWYo"
    "Im9ic2VydmFibGVDb3VudCIsICZTaWduYWxzOjpvYnNlcnZhYmxlQ291bnQpCisgICAgICAgIC5k"
    "ZWYoIl9odHNDaGlzcVdpdGhNZWFzdXJlbWVudEJyZWFrZG93biIsCisgICAgICAgICAgICAgJlNp"
    "Z25hbHM6OmNoaXNxV2l0aE1lYXN1cmVtZW50QnJlYWtkb3duLCAicHJlZGljdGlvbnMiX2EsCisg"
    "ICAgICAgICAgICAgIm1vZGlmaWNhdGlvbkZhY3RvcnMiX2EgPSBTaWduYWxzOjpNb2RpZmljYXRp"
    "b25GYWN0b3Jze30pCiAgICAgICAgIC5kZWYoIm1lYXN1cmVtZW50cyIsICZTaWduYWxzOjptZWFz"
    "dXJlbWVudHMsCiAgICAgICAgICAgICAgcHk6OnJldHVybl92YWx1ZV9wb2xpY3k6OnJlZmVyZW5j"
    "ZV9pbnRlcm5hbCkKICAgICAgICAgLmRlZigiX19yZXByX18iLCBbXShjb25zdCBTaWduYWxzICZz"
    "ZWxmKSB7Cg=="
""")

ASSIGNMENT_MARKERS = (
    ("python/Higgs/HiggsSignals.cpp", "_htsAssignmentTraceVersion"),
    ("src/signals/Measurements/RateMeasurement.cpp", "assignedCouplingParticles"),
)
BREAKDOWN_MARKERS = (
    ("include/Higgs/Signals.hpp", "chisqWithMeasurementBreakdown"),
    ("python/Higgs/HiggsSignals.cpp", "_htsMeasurementBreakdownVersion"),
)


def _present(source, markers):
    found, missing = [], []
    for relative, token in markers:
        file = source / relative
        if not file.is_file():
            raise SystemExit(f"Bridge source file is missing: {relative}")
        if token in file.read_text(encoding="utf-8"):
            found.append(relative)
        else:
            missing.append(relative)
    if found and missing:
        raise SystemExit("HiggsTools bridge is only partly applied: " + ", ".join(missing))
    return bool(found)


def _apply(source, payload, digest, label):
    actual = hashlib.sha256(payload).hexdigest()
    if actual != digest:
        raise SystemExit(f"{label} SHA256 mismatch")
    temporary = tempfile.NamedTemporaryFile(prefix=label + "-", suffix=".patch", delete=False)
    try:
        temporary.write(payload)
        temporary.close()
        subprocess.run(
            ["patch", "--batch", "--fuzz=0", "-p1", "-i", temporary.name],
            cwd=source, check=True,
        )
    finally:
        os.unlink(temporary.name)


def main(argv):
    if len(argv) != 2:
        raise SystemExit("usage: higgstools_prepare.py SOURCE_DIR")
    source = Path(argv[1]).resolve()
    cmake = source / "CMakeLists.txt"
    if not cmake.is_file() or "VERSION 1.1.3" not in cmake.read_text(encoding="utf-8"):
        raise SystemExit("Assignment bridge requires HiggsTools 1.1.3 source")
    if _present(source, ASSIGNMENT_MARKERS):
        print("assignment-trace v2 already applied", flush=True)
    else:
        _apply(source, ASSIGNMENT, ASSIGNMENT_SHA256, "assignment-trace-v2")
        if not _present(source, ASSIGNMENT_MARKERS):
            raise SystemExit("Assignment-trace v2 did not apply")
        print("applied assignment-trace v2", flush=True)
    if _present(source, BREAKDOWN_MARKERS):
        print("measurement-breakdown v1 already applied", flush=True)
    else:
        _apply(source, BREAKDOWN, BREAKDOWN_SHA256, "measurement-breakdown-v1")
        if not _present(source, BREAKDOWN_MARKERS):
            raise SystemExit("HS measurement-breakdown v1 did not apply")
        print("applied measurement-breakdown v1", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"patch failed with exit {exc.returncode}") from exc
