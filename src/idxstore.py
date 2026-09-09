"""ctypes bindings for libIndexStore.dylib (LLVM/Swift index store reader)."""
import ctypes, os, subprocess
from ctypes import c_bool, c_char_p, c_size_t, c_uint, c_uint64, c_void_p, POINTER, byref

def _dylib_path():
    override = os.getenv("LIBINDEXSTORE")
    if override:
        return override
    dev = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True).stdout.strip()
    return f"{dev}/Toolchains/XcodeDefault.xctoolchain/usr/lib/libIndexStore.dylib"

class StringRef(ctypes.Structure):
    _fields_ = [("data", c_void_p), ("length", c_size_t)]
    def str(self):
        if not self.data or not self.length:
            return ""
        return ctypes.string_at(self.data, self.length).decode("utf-8", "replace")

lib = ctypes.CDLL(_dylib_path())

UNIT_APPLIER = ctypes.CFUNCTYPE(c_bool, c_void_p, StringRef)
DEP_APPLIER = ctypes.CFUNCTYPE(c_bool, c_void_p, c_void_p)
OCC_APPLIER = ctypes.CFUNCTYPE(c_bool, c_void_p, c_void_p)
REL_APPLIER = ctypes.CFUNCTYPE(c_bool, c_void_p, c_void_p)

_sig = {
    "indexstore_format_version": (c_uint, []),
    "indexstore_store_create": (c_void_p, [c_char_p, POINTER(c_void_p)]),
    "indexstore_store_dispose": (None, [c_void_p]),
    "indexstore_error_get_description": (StringRef, [c_void_p]),
    "indexstore_error_dispose": (None, [c_void_p]),
    "indexstore_store_units_apply_f": (c_bool, [c_void_p, c_uint, c_void_p, UNIT_APPLIER]),
    "indexstore_unit_reader_create": (c_void_p, [c_void_p, c_char_p, POINTER(c_void_p)]),
    "indexstore_unit_reader_dispose": (None, [c_void_p]),
    "indexstore_unit_reader_get_module_name": (StringRef, [c_void_p]),
    "indexstore_unit_reader_get_main_file": (StringRef, [c_void_p]),
    "indexstore_unit_reader_get_output_file": (StringRef, [c_void_p]),
    "indexstore_unit_reader_get_target": (StringRef, [c_void_p]),
    "indexstore_unit_reader_get_working_dir": (StringRef, [c_void_p]),
    "indexstore_unit_reader_is_system_unit": (c_bool, [c_void_p]),
    "indexstore_unit_reader_is_module_unit": (c_bool, [c_void_p]),
    "indexstore_unit_reader_is_debug_compilation": (c_bool, [c_void_p]),
    "indexstore_unit_reader_dependencies_apply_f": (c_bool, [c_void_p, c_void_p, DEP_APPLIER]),
    "indexstore_unit_dependency_get_kind": (c_uint, [c_void_p]),
    "indexstore_unit_dependency_get_filepath": (StringRef, [c_void_p]),
    "indexstore_unit_dependency_get_name": (StringRef, [c_void_p]),
    "indexstore_unit_dependency_get_modulename": (StringRef, [c_void_p]),
    "indexstore_unit_dependency_is_system": (c_bool, [c_void_p]),
    "indexstore_record_reader_create": (c_void_p, [c_void_p, c_char_p, POINTER(c_void_p)]),
    "indexstore_record_reader_dispose": (None, [c_void_p]),
    "indexstore_record_reader_occurrences_apply_f": (c_bool, [c_void_p, c_void_p, OCC_APPLIER]),
    "indexstore_occurrence_get_symbol": (c_void_p, [c_void_p]),
    "indexstore_occurrence_get_roles": (c_uint64, [c_void_p]),
    "indexstore_occurrence_get_line_col": (None, [c_void_p, POINTER(c_uint), POINTER(c_uint)]),
    "indexstore_occurrence_relations_apply_f": (c_bool, [c_void_p, c_void_p, REL_APPLIER]),
    "indexstore_symbol_relation_get_roles": (c_uint64, [c_void_p]),
    "indexstore_symbol_relation_get_symbol": (c_void_p, [c_void_p]),
    "indexstore_symbol_get_kind": (c_uint, [c_void_p]),
    "indexstore_symbol_get_subkind": (c_uint, [c_void_p]),
    "indexstore_symbol_get_language": (c_uint, [c_void_p]),
    "indexstore_symbol_get_properties": (c_uint64, [c_void_p]),
    "indexstore_symbol_get_roles": (c_uint64, [c_void_p]),
    "indexstore_symbol_get_name": (StringRef, [c_void_p]),
    "indexstore_symbol_get_usr": (StringRef, [c_void_p]),
}
for name, (res, args) in _sig.items():
    fn = getattr(lib, name)
    fn.restype = res
    fn.argtypes = args

DEP_UNIT, DEP_RECORD, DEP_FILE = 1, 2, 3

SYMBOL_KIND = {
    0: "Unknown", 1: "Module", 2: "Namespace", 3: "NamespaceAlias", 4: "Macro",
    5: "Enum", 6: "Struct", 7: "Class", 8: "Protocol", 9: "Extension", 10: "Union",
    11: "TypeAlias", 12: "Function", 13: "Variable", 14: "Field", 15: "EnumConstant",
    16: "InstanceMethod", 17: "ClassMethod", 18: "StaticMethod", 19: "InstanceProperty",
    20: "ClassProperty", 21: "StaticProperty", 22: "Constructor", 23: "Destructor",
    24: "ConversionFunction", 25: "Parameter", 26: "Using", 27: "Concept",
    1000: "CommentTag",
}
LANGUAGE = {0: "C", 1: "ObjC", 2: "C++", 100: "Swift"}

ROLE = {
    1 << 0: "declaration", 1 << 1: "definition", 1 << 2: "reference",
    1 << 3: "read", 1 << 4: "write", 1 << 5: "call", 1 << 6: "dynamic",
    1 << 7: "addressof", 1 << 8: "implicit",
    1 << 9: "childOf", 1 << 10: "baseOf", 1 << 11: "overrideOf",
    1 << 12: "receivedBy", 1 << 13: "calledBy", 1 << 14: "extendedBy",
    1 << 15: "accessorOf", 1 << 16: "containedBy", 1 << 17: "ibTypeOf",
    1 << 18: "specializationOf", 1 << 19: "undefinition",
}

def role_names(mask):
    return [n for bit, n in sorted(ROLE.items()) if mask & bit]

def store_open(path):
    err = c_void_p()
    st = lib.indexstore_store_create(path.encode(), byref(err))
    if not st:
        msg = lib.indexstore_error_get_description(err).str() if err else "unknown error"
        raise RuntimeError(f"cannot open index store at {path}: {msg}")
    return st

def unit_names(store, sort=True):
    out = []
    @UNIT_APPLIER
    def cb(_ctx, sref):
        out.append(sref.str())
        return True
    lib.indexstore_store_units_apply_f(store, 1 if sort else 0, None, cb)
    return out
