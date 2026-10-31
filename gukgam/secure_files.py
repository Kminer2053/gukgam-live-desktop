"""링크를 따라가지 않는 비공개 파일 I/O. 지원하지 않는 보호는 실패로 닫는다.

POSIX는 디렉터리 FD와 O_NOFOLLOW를 사용한다. Windows는 각 부모의 삭제
공유를 막은 핸들을 수명 동안 보유하고, 재분석 지점을 거부한다. 비공개 새
파일/폴더는 현재 사용자와 SYSTEM만 허용하는 보호 DACL로 생성한다.
"""
from __future__ import annotations

import contextlib
import ctypes
import io
import os
from pathlib import Path
import re
import stat
import threading


_WINDOWS = os.name == "nt"
_SEEK_LOCK = threading.RLock()


def _path(value) -> Path:
    value = Path(value).expanduser()
    if ".." in value.parts:
        raise ValueError("안전한 파일 경로에 상위 이동을 사용할 수 없습니다")
    value = Path(os.path.abspath(value))
    if _WINDOWS:
        if not re.fullmatch(r"[A-Za-z]:", value.drive) or value.root != "\\":
            raise ValueError("보호된 파일은 로컬 드라이브의 절대 경로여야 합니다")
        for part in value.parts[1:]:
            if (not part or part.endswith((".", " ")) or any(c in part for c in ':<>"|?*\0')
                    or re.fullmatch(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", part, re.I)):
                raise ValueError("보호된 파일 경로에 장치명이나 대체 스트림을 사용할 수 없습니다")
            if len(part.encode("utf-16-le")) + 2 > 0xFFFF:
                raise ValueError("보호된 파일 이름의 UTF-16 길이 한도를 넘었습니다")
    return value


class _Windows:
    """Win32 API는 Windows에서 처음 사용할 때만 연결한다. 비밀값은 출력하지 않는다."""
    def __init__(self):
        from ctypes import wintypes as w
        self.w = w
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.a = ctypes.WinDLL("advapi32", use_last_error=True)
        self.n = ctypes.WinDLL("ntdll", use_last_error=True)
        self._pending = []
        self._pending_lock = threading.RLock()
        p, d, h = ctypes.c_void_p, w.DWORD, w.HANDLE

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("length", d), ("descriptor", p), ("inherit", w.BOOL)]

        class FileInformation(ctypes.Structure):
            _fields_ = [("attributes", d), ("created", w.FILETIME), ("accessed", w.FILETIME),
                        ("written", w.FILETIME), ("volume", d), ("size_high", d), ("size_low", d),
                        ("links", d), ("index_high", d), ("index_low", d)]

        class AclInformation(ctypes.Structure):
            _fields_ = [("count", d), ("used", d), ("free", d)]

        class RenameInformation(ctypes.Structure):
            _fields_ = [("replace", ctypes.c_ubyte), ("root", h), ("length", d), ("name", w.WCHAR * 1)]

        class UnicodeString(ctypes.Structure):
            _fields_ = [("length", w.USHORT), ("maximum", w.USHORT), ("buffer", w.LPWSTR)]

        class ObjectAttributes(ctypes.Structure):
            _fields_ = [("length", d), ("root", h), ("name", ctypes.POINTER(UnicodeString)),
                        ("attributes", d), ("security", p), ("quality", p)]

        class IoStatus(ctypes.Structure):
            _fields_ = [("status_or_pointer", p), ("information", ctypes.c_size_t)]

        self.SecurityAttributes, self.FileInformation = SecurityAttributes, FileInformation
        self.AclInformation, self.RenameInformation = AclInformation, RenameInformation
        self.UnicodeString, self.ObjectAttributes, self.IoStatus = UnicodeString, ObjectAttributes, IoStatus
        signatures = [
            (self.k.CreateFileW, [w.LPCWSTR, d, d, p, d, d, h], h),
            (self.k.CreateDirectoryW, [w.LPCWSTR, p], w.BOOL),
            (self.k.CloseHandle, [h], w.BOOL),
            (self.k.GetFileType, [h], d),
            (self.k.GetFileInformationByHandle, [h, p], w.BOOL),
            (self.k.GetVolumeInformationByHandleW, [h, w.LPWSTR, d, p, p, p, w.LPWSTR, d], w.BOOL),
            (self.k.SetFileInformationByHandle, [h, ctypes.c_int, p, d], w.BOOL),
            (self.k.WaitForSingleObject, [h, d], d),
            (self.k.GetCurrentProcess, [], h),
            (self.k.LocalFree, [p], p),
            (self.a.OpenProcessToken, [h, d, p], w.BOOL),
            (self.a.GetTokenInformation, [h, ctypes.c_int, p, d, p], w.BOOL),
            (self.a.ConvertSidToStringSidW, [p, p], w.BOOL),
            (self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW, [w.LPCWSTR, d, p, p], w.BOOL),
            (self.a.GetSecurityInfo, [h, ctypes.c_int, d, p, p, p, p, p], d),
            (self.a.GetSecurityDescriptorControl, [p, p, p], w.BOOL),
            (self.a.GetAclInformation, [p, p, d, ctypes.c_int], w.BOOL),
            (self.a.GetAce, [p, d, p], w.BOOL),
            (self.n.NtCreateFile, [p, d, p, p, p, d, d, d, d, p, d], ctypes.c_long),
            (self.n.NtSetInformationFile, [h, p, p, d, ctypes.c_int], ctypes.c_long),
            (self.n.RtlNtStatusToDosError, [ctypes.c_long], d),
        ]
        for function, args, result in signatures:
            function.argtypes, function.restype = args, result

    def error(self):
        raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def status(value):
        return ctypes.c_long.from_buffer(value).value

    def reap_pending(self):
        """완료되지 않은 커널 출력 버퍼는 해제하지 않는다. 새 실행은 최대 8건에서 닫는다."""
        with self._pending_lock:
            waiting = []
            for item in self._pending:
                if self.status(item["status"]) == 0x103:
                    waiting.append(item)
                    continue
                handle = item.get("handle")
                if handle is not None and handle.value not in (None, ctypes.c_void_p(-1).value):
                    self.k.CloseHandle(handle)
                attributes = item.get("attributes")
                if attributes is not None:
                    self.k.LocalFree(attributes.descriptor)
            self._pending = waiting
            if len(waiting) >= 8:
                raise OSError("완료되지 않은 안전 파일 작업이 있어 새 작업을 보류합니다")

    def retain_pending(self, status, **values):
        attributes = values.get("attributes")
        if attributes is not None:
            attributes._deferred = True
        with self._pending_lock:
            self._pending.append({"status": status, **values})

    def sid_text(self, sid):
        text = ctypes.c_void_p()
        if not self.a.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            self.error()
        try:
            return ctypes.wstring_at(text)
        finally:
            self.k.LocalFree(text)

    def user_sid(self):
        token = self.w.HANDLE()
        if not self.a.OpenProcessToken(self.k.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            self.error()
        try:
            size = self.w.DWORD()
            self.a.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not size.value:
                self.error()
            data = ctypes.create_string_buffer(size.value)
            if not self.a.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
                self.error()
            return self.sid_text(ctypes.c_void_p.from_buffer(data).value)
        finally:
            self.k.CloseHandle(token)

    @contextlib.contextmanager
    def security(self, directory=False):
        sid = self.user_sid()
        inheritance = "OICI" if directory else ""
        descriptor = ctypes.c_void_p()
        sddl = f"O:{sid}D:P(A;{inheritance};FA;;;{sid})(A;{inheritance};FA;;;SY)"
        if not self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            self.error()
        try:
            attributes = self.SecurityAttributes(ctypes.sizeof(self.SecurityAttributes), descriptor, False)
            attributes._deferred = False
            yield attributes
        finally:
            if not attributes._deferred:
                self.k.LocalFree(descriptor)

    def info(self, handle, *, directory=False):
        value = self.FileInformation()
        if self.k.GetFileType(handle) != 1 or not self.k.GetFileInformationByHandle(handle, ctypes.byref(value)):
            raise ValueError("안전한 디스크 파일의 열린 핸들이 필요합니다")
        if value.attributes & 0x400 or bool(value.attributes & 0x10) != directory:
            raise ValueError("재분석 지점 또는 다른 형식의 파일을 거부했습니다")
        if not value.index_high and not value.index_low:
            raise ValueError("열린 파일의 고유 식별자를 확인할 수 없습니다")
        return value

    def identity(self, handle, *, directory=False):
        value = self.info(handle, directory=directory)
        return value.volume, value.index_high, value.index_low

    def persistent_acls(self, handle):
        flags = self.w.DWORD()
        if not self.k.GetVolumeInformationByHandleW(handle, None, 0, None, None, ctypes.byref(flags), None, 0):
            self.error()
        if not flags.value & 0x8:
            raise ValueError("비공개 파일은 접근 권한을 보존하는 파일 시스템에만 저장합니다")

    def open(self, path, **options):
        with self._pending_lock:
            self.reap_pending()
            return self._open(path, **options)

    def _open(self, path, *, parent=None, directory=False, new=False, read_write=False, append=False,
             delete=False, shared_delete=False, read_control=False):
        # 부모 탐색에는 속성 읽기만 요청한다. ACL 조회는 검사할 소유 항목에만 요청한다.
        access = (0x80 | (0x20000 if new or read_control else 0)) if directory else 0x120089 | (0x120116 if read_write or new else 0)
        if append:
            access = 0x120084  # FILE_APPEND_DATA만 허용해 임의 위치의 기존 내용을 덮지 않는다.
        if delete:
            access |= 0x10000
        share = (1 if directory else 3) | (4 if shared_delete else 0)
        if parent is None:
            # 드라이브 루트만 절대 이름으로 연다. 하위 성분은 항상 열린 부모 기준이다.
            if new or not directory or Path(path) != Path(path.anchor):
                raise ValueError("하위 파일은 검사한 부모 핸들을 기준으로 열어야 합니다")
            handle = self.k.CreateFileW(str(path), access, share, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                self.error()
        else:
            name = str(Path(path).name)
            encoded = name.encode("utf-16-le")
            if len(encoded) + 2 > 0xFFFF:
                raise ValueError("보호된 파일 이름의 UTF-16 길이 한도를 넘었습니다")
            text = ctypes.create_unicode_buffer(name)
            unicode = self.UnicodeString(len(encoded), len(encoded) + 2, ctypes.cast(text, self.w.LPWSTR))
            result, status = self.w.HANDLE(), self.IoStatus(0x103, 0)
            with self.security(directory) if new else contextlib.nullcontext(None) as attributes:
                object_attributes = self.ObjectAttributes(ctypes.sizeof(self.ObjectAttributes), parent,
                    ctypes.pointer(unicode), 0x1040, attributes.descriptor if attributes is not None else None, None)
                # OBJ_DONT_REPARSE + FILE_OPEN_REPARSE_POINT와 단일 상대 성분을 함께 사용한다.
                code = self.n.NtCreateFile(ctypes.byref(result), access, ctypes.byref(object_attributes),
                    ctypes.byref(status), None, 0x10 if directory else 0x80, share,
                    2 if new else 1, 0x00200000 | (1 if directory else 0x60), None, 0)
                if code == 0x103:
                    self.retain_pending(status, handle=result, text=text, name=unicode,
                                        object_attributes=object_attributes, attributes=attributes)
                    raise OSError("안전한 파일 열기의 완료를 확인하지 못했습니다. 자동 재시도하지 않습니다")
            if code < 0:
                raise ctypes.WinError(self.n.RtlNtStatusToDosError(code))
            handle = result.value
        try:
            self.info(handle, directory=directory)
        except BaseException:
            self.k.CloseHandle(handle)
            raise
        return handle

    def verify_private(self, handle, *, directory=False):
        self.info(handle, directory=directory)
        self.persistent_acls(handle)
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        code = self.a.GetSecurityInfo(handle, 1, 0x5, ctypes.byref(owner), None,
                                      ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if code:
            raise ctypes.WinError(code)
        try:
            user = self.user_sid()
            if not owner.value or self.sid_text(owner) != user or not dacl.value:
                raise ValueError("현재 사용자만 관리하는 비공개 접근 권한이 필요합니다")
            control, revision = self.w.WORD(), self.w.DWORD()
            if not self.a.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
                self.error()
            if not control.value & 0x1000 or not control.value & 0x4:
                raise ValueError("상위 폴더의 공개 권한을 상속하지 않는 보호 DACL이 필요합니다")
            acl = self.AclInformation()
            if not self.a.GetAclInformation(dacl, ctypes.byref(acl), ctypes.sizeof(acl), 2):
                self.error()
            current_user_full = False
            for index in range(acl.count):
                ace = ctypes.c_void_p()
                if not self.a.GetAce(dacl, index, ctypes.byref(ace)):
                    self.error()
                header = ctypes.string_at(ace, 4)
                if header[0] != 0 or header[1] & 0x18 or int.from_bytes(header[2:4], "little") < 12:
                    raise ValueError("확인되지 않은 비공개 접근 권한 형식입니다")
                principal = self.sid_text(ace.value + 8)
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                if principal not in (user, "S-1-5-18"):
                    raise ValueError("비공개 파일에 다른 사용자의 접근 권한이 있습니다")
                current_user_full |= principal == user and mask & 0x1F01FF == 0x1F01FF
            if not current_user_full:
                raise ValueError("현재 사용자의 비공개 파일 제어 권한을 확인할 수 없습니다")
        finally:
            self.k.LocalFree(descriptor)

    def mkdir(self, path, parent):
        handle = self.open(path, parent=parent, directory=True, new=True)
        try:
            self.verify_private(handle, directory=True)
        finally:
            self.k.CloseHandle(handle)

    def rename(self, handle, parent, name):
        with self._pending_lock:
            self.reap_pending()
            return self._rename(handle, parent, name)

    def _rename(self, handle, parent, name):
        encoded = name.encode("utf-16-le")
        offset = self.RenameInformation.name.offset
        buffer = ctypes.create_string_buffer(ctypes.sizeof(self.RenameInformation) + len(encoded) + 2)
        value = self.RenameInformation.from_buffer(buffer)
        # 같은 폴더 안의 단일 이름 변경은 원본 파일 핸들의 부모를 기준으로 한다.
        value.replace, value.root, value.length = 1, None, len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
        status = self.IoStatus(0x103, 0)
        code = self.n.NtSetInformationFile(handle, ctypes.byref(status), buffer, len(buffer), 10)
        if code == 0x103:
            # 원본 파일은 SYNCHRONIZE + FILE_SYNCHRONOUS_IO_NONALERT로 열린 소유 핸들이다.
            waited = self.k.WaitForSingleObject(handle, 0xFFFFFFFF)
            if waited != 0 or self.status(status) == 0x103:
                self.retain_pending(status, buffer=buffer)
                raise OSError("원자 교체의 완료 상태를 확인하지 못했습니다")
            code = self.status(status)
        if code < 0:
            raise ctypes.WinError(self.n.RtlNtStatusToDosError(code))

    def unlink(self, handle):
        remove = ctypes.c_ubyte(1)
        if not self.k.SetFileInformationByHandle(handle, 4, ctypes.byref(remove), ctypes.sizeof(remove)):
            self.error()


_WIN = None


def _windows():
    global _WIN
    if _WIN is None:
        _WIN = _Windows()
    return _WIN


class _Parents:
    """열린 부모를 역순으로 닫는다. Windows에서는 이름 교체도 막는다."""
    def __init__(self, path: Path, *, create=False):
        self.path, self.opened = _path(path), []
        try:
            if _WINDOWS:
                api = _windows()
                current = Path(self.path.anchor)
                self.opened.append((current, api.open(current, directory=True)))
                for name in self.path.parts[1:]:
                    current = current / name
                    try:
                        handle = api.open(current, parent=self.fd, directory=True)
                    except FileNotFoundError:
                        if not create:
                            raise
                        handle = api.open(current, parent=self.fd, directory=True, new=True)
                        try:
                            api.verify_private(handle, directory=True)
                        except BaseException:
                            api.k.CloseHandle(handle)
                            raise
                    self.opened.append((current, handle))
            else:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                self.opened.append((Path(self.path.anchor), os.open(self.path.anchor, flags)))
                for name in self.path.parts[1:]:
                    try:
                        fd = os.open(name, flags, dir_fd=self.fd)
                    except FileNotFoundError:
                        if not create:
                            raise
                        os.mkdir(name, 0o700, dir_fd=self.fd)
                        fd = os.open(name, flags, dir_fd=self.fd)
                    self.opened.append((self.opened[-1][0] / name, fd))
            self.validate()
        except BaseException:
            self.close()
            raise

    @property
    def fd(self):
        return self.opened[-1][1]

    def validate(self):
        for index, (path, descriptor) in enumerate(self.opened):
            if _WINDOWS:
                api = _windows()
                expected = api.identity(descriptor, directory=True)
                check = api.open(path, parent=self.opened[index - 1][1] if index else None, directory=True)
                try:
                    if api.identity(check, directory=True) != expected:
                        raise ValueError("보호된 상위 폴더가 교체되었습니다")
                finally:
                    api.k.CloseHandle(check)
            else:
                current, opened = path.stat(follow_symlinks=False), os.fstat(descriptor)
                if (not stat.S_ISDIR(current.st_mode)
                        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
                    raise ValueError("보호된 상위 폴더가 교체되었습니다")

    def close(self):
        for _path_value, value in reversed(self.opened):
            if _WINDOWS:
                _windows().k.CloseHandle(value)
            else:
                os.close(value)
        self.opened.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _write_fully(write, raw) -> int:
    view = memoryview(raw).cast("B")
    size = len(view)
    if not size:
        count = write(view)
        if type(count) is not int or count != 0:
            raise OSError("빈 파일 기록의 반환 길이가 잘못됐습니다")
        return 0
    while view:
        count = write(view)
        if type(count) is not int or count <= 0 or count > len(view):
            raise OSError("비공개 파일을 모두 기록하지 못했습니다")
        view = view[count:]
    return size


def write_all(stream, raw) -> None:
    """짧은 쓰기를 이어 기록하고 진전 없는 쓰기는 실패로 반환한다."""
    _write_fully(stream.write, raw)


class SecureFile(io.FileIO):
    """파일이 닫힐 때까지 부모 핸들의 보호도 유지하는 이진 스트림이다."""
    def __init__(self, fd, mode, parents):
        # 초기화가 성공하기 전까지 FD 소유자는 _open_file 하나다.
        self._parents = None
        self._owned_fd = None
        super().__init__(fd, mode, closefd=False)
        self._owned_fd = fd
        self._parents = parents

    def _write_once(self, raw):
        return super().write(raw)

    def write(self, raw):
        # wave·zipfile 등 반환 길이를 검사하지 않는 소비자도 전량 기록한다.
        return _write_fully(self._write_once, raw)

    def close(self):
        owned, parents = self._owned_fd, self._parents
        self._owned_fd, self._parents = None, None
        try:
            super().close()
        finally:
            try:
                if owned is not None:
                    os.close(owned)
            finally:
                if parents is not None:
                    parents.close()


def _open_file(path, *, new=False, read_write=False, append=False, private=False) -> SecureFile:
    path = _path(path)
    parents = _Parents(path.parent)
    fd = None
    try:
        if _WINDOWS:
            import msvcrt
            api = _windows()
            handle = api.open(path, parent=parents.fd, new=new, read_write=read_write, append=append)
            try:
                if private or new:
                    api.verify_private(handle)
                expected = api.identity(handle)
                check = api.open(path, parent=parents.fd)
                try:
                    if api.identity(check) != expected:
                        raise ValueError("검사 중 파일이 교체되었습니다")
                finally:
                    api.k.CloseHandle(check)
                fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_WRONLY | os.O_APPEND if append
                                          else os.O_RDWR if new or read_write else os.O_RDONLY))
            except BaseException:
                api.k.CloseHandle(handle)
                raise
        else:
            flags = (os.O_RDWR if read_write else os.O_WRONLY) if new else os.O_WRONLY | os.O_APPEND if append else os.O_RDONLY
            flags |= os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            if new:
                flags |= os.O_CREAT | os.O_EXCL
            fd = os.open(path.name, flags, 0o600, dir_fd=parents.fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("입력과 출력은 일반 파일이어야 합니다")
            if private or new:
                verify_private_file(fd)
            linked = os.stat(path.name, dir_fd=parents.fd, follow_symlinks=False)
            if (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("검사 중 파일이 교체되었습니다")
        parents.validate()
        result = SecureFile(fd, "a" if append else "r+" if new and read_write else "w" if new else "r", parents)
        fd = None
        return result
    except BaseException:
        if fd is not None:
            os.close(fd)
        parents.close()
        raise


def create_private_file(path, *, read_write=False) -> SecureFile:
    """존재하는 이름을 덮어쓰지 않고 비공개 일반 파일을 원자 생성한다."""
    return _open_file(path, new=True, read_write=read_write)


def open_regular_file(path, *, private=False):
    """일반 파일만 읽는다. private=True이면 소유자 전용 접근 권한도 확인한다."""
    return io.BufferedReader(_open_file(path, private=private))


def append_private_file(path):
    """비공개 일반 파일에만 원자 append한다. 없으면 CREATE_NEW로 준비한다."""
    try:
        stream = _open_file(path, append=True, private=True)
    except FileNotFoundError:
        try:
            with create_private_file(path):
                pass
        except FileExistsError:
            pass
        stream = _open_file(path, append=True, private=True)
    return io.BufferedWriter(stream)


def verify_private_file(path_or_fd, *, directory=False) -> bool:
    """기존 자료의 권한을 고치지 않고 열린 핸들의 소유자·접근 범위를 확인한다."""
    if hasattr(path_or_fd, "fileno"):
        path_or_fd = path_or_fd.fileno()
    if isinstance(path_or_fd, bool):
        raise ValueError("검사할 파일 경로나 열린 파일 핸들이 필요합니다")
    if not isinstance(path_or_fd, int):
        path = _path(path_or_fd)
        if directory:
            with _Parents(path) as parents:
                if _WINDOWS:
                    _verify_directory(parents)
                else:
                    verify_private_file(parents.fd, directory=True)
        else:
            with open_regular_file(path, private=True):
                pass
        return True
    if _WINDOWS:
        import msvcrt
        _windows().verify_private(msvcrt.get_osfhandle(path_or_fd), directory=directory)
    else:
        info = os.fstat(path_or_fd)
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        if (not kind(info.st_mode) or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
                or info.st_uid != os.getuid()):
            raise ValueError("현재 사용자 소유의 비공개 일반 파일 또는 폴더가 필요합니다")
    return True


def _verify_directory(parents):
    api = _windows()
    if len(parents.opened) < 2:
        raise ValueError("드라이브 루트를 비공개 자료 폴더로 사용할 수 없습니다")
    path, held = parents.opened[-1]
    handle = api.open(path, parent=parents.opened[-2][1], directory=True, read_control=True)
    try:
        if api.identity(handle, directory=True) != api.identity(held, directory=True):
            raise ValueError("권한 검사 중 비공개 폴더가 교체되었습니다")
        api.verify_private(handle, directory=True)
    finally:
        api.k.CloseHandle(handle)


def make_private_directory(path, *, parents=False, exist_ok=False) -> Path:
    """새 폴더에만 비공개 권한을 부여한다. 기존 폴더의 권한은 변경하지 않는다."""
    path = _path(path)
    with _Parents(path.parent, create=parents) as guard:
        made = False
        try:
            if _WINDOWS:
                _windows().mkdir(path, guard.fd)
            else:
                os.mkdir(path.name, 0o700, dir_fd=guard.fd)
            made = True
        except FileExistsError:
            if not exist_ok:
                raise
        with _Parents(path) as final:
            if made:
                if _WINDOWS:
                    _verify_directory(final)
                else:
                    verify_private_file(final.fd, directory=True)
            guard.validate()
    return path


def atomic_replace(source, target) -> None:
    """같은 안전한 부모 안에서 비공개 새 파일을 원자 교체한다. 링크는 거부한다."""
    source, target = _path(source), _path(target)
    if source.parent != target.parent or source.name == target.name:
        raise ValueError("원자 교체는 같은 폴더의 서로 다른 파일만 허용합니다")
    with _Parents(source.parent) as parents:
        if _WINDOWS:
            api = _windows()
            handle = api.open(source, parent=parents.fd, delete=True)
            try:
                api.verify_private(handle)
                identity = api.identity(handle)
                try:
                    check = api.open(target, parent=parents.fd)
                except FileNotFoundError:
                    check = None
                if check is not None:
                    try:
                        api.verify_private(check)
                    finally:
                        api.k.CloseHandle(check)
                parents.validate()
                # 열린 원본 핸들을 상대 이름으로 바꾸므로 원본 교체 경합을 따라가지 않는다.
                api.rename(handle, parents.fd, target.name)
                check = api.open(target, parent=parents.fd, shared_delete=True)
                try:
                    if api.identity(check) != identity:
                        raise ValueError("원자 교체 결과의 열린 파일이 다릅니다")
                    api.verify_private(check)
                finally:
                    api.k.CloseHandle(check)
            finally:
                api.k.CloseHandle(handle)
        else:
            with open_regular_file(source, private=True):
                try:
                    with open_regular_file(target, private=True):
                        pass
                except FileNotFoundError:
                    pass
                parents.validate()
                os.replace(source.name, target.name, src_dir_fd=parents.fd, dst_dir_fd=parents.fd)


def unlink_owned_file(path, identity: tuple[int, int]) -> None:
    """실패 정리도 같은 열린 일반 파일일 때만 수행한다. 링크와 교체 파일은 남긴다."""
    path = _path(path)
    with _Parents(path.parent) as parents:
        if _WINDOWS:
            import msvcrt
            api = _windows()
            handle = api.open(path, parent=parents.fd, delete=True)
            fd = None
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != identity:
                    raise ValueError("정리하려는 파일이 이번 실행의 소유 파일과 다릅니다")
                parents.validate()
                api.unlink(handle)
            finally:
                if fd is not None:
                    os.close(fd)
                else:
                    api.k.CloseHandle(handle)
        else:
            value = os.stat(path.name, dir_fd=parents.fd, follow_symlinks=False)
            if not stat.S_ISREG(value.st_mode) or (value.st_dev, value.st_ino) != identity:
                raise ValueError("정리하려는 파일이 이번 실행의 소유 파일과 다릅니다")
            parents.validate()
            os.unlink(path.name, dir_fd=parents.fd)


def positional_write(fd, data, offset):
    """POSIX pwrite를 보존하고 Windows에서는 호출자의 직렬화 안에서 위치를 복원한다."""
    if hasattr(os, "pwrite"):
        return os.pwrite(fd, data, offset)
    with _SEEK_LOCK:
        previous = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            return os.write(fd, data)
        finally:
            os.lseek(fd, previous, os.SEEK_SET)


def positional_read(fd, count, offset):
    """Windows의 단일 소유 PCM 파일에서도 전체 파일을 복사하지 않고 읽는다."""
    if hasattr(os, "pread"):
        return os.pread(fd, count, offset)
    with _SEEK_LOCK:
        previous = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            return os.read(fd, count)
        finally:
            os.lseek(fd, previous, os.SEEK_SET)
