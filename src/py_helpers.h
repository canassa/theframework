/*
 * Minimal C helpers for Python and greenlet C API operations that
 * Zig's @cImport cannot translate (macro-based constants, function-pointer
 * tables, inline functions, etc.).
 *
 * This file is compiled as C and linked into the Zig extension module.
 */
#ifndef PY_HELPERS_H
#define PY_HELPERS_H

#include <Python.h>

/* --- Python constants that are macros --- */
PyObject *py_helper_none(void);
void py_helper_incref(PyObject *o);
void py_helper_decref(PyObject *o);
int py_helper_meth_noargs(void);
int py_helper_meth_varargs(void);

/* --- Module creation wrappers --- */
PyMethodDef py_helper_method_sentinel(void);
PyObject *py_helper_module_create(PyModuleDef *def);
PyModuleDef_Base py_helper_module_def_head_init(void);

/* --- Greenlet C API wrappers --- */

/* Initialize the greenlet C API function-pointer table.
 * Must be called once during module init before any other greenlet call.
 * Returns 0 on success, -1 on failure (with Python exception set). */
int py_helper_greenlet_import(void);

/* greenlet.getcurrent() — returns a NEW reference. */
PyObject *py_helper_greenlet_getcurrent(void);

/* greenlet.greenlet(run, parent) — returns a NEW reference.
 * parent may be NULL (defaults to current greenlet). */
PyObject *py_helper_greenlet_new(PyObject *run, PyObject *parent);

/* g.switch(*args, **kwargs) — returns a NEW reference.
 * args and kwargs may be NULL. */
PyObject *py_helper_greenlet_switch(PyObject *greenlet, PyObject *args, PyObject *kwargs);

/* Query greenlet state. */
int py_helper_greenlet_active(PyObject *greenlet);
int py_helper_greenlet_started(PyObject *greenlet);
int py_helper_greenlet_main(PyObject *greenlet);

/* Tuple helpers */
PyObject *py_helper_tuple_new(Py_ssize_t size);
int py_helper_tuple_setitem(PyObject *tuple, Py_ssize_t pos, PyObject *item);
PyObject *py_helper_tuple_getitem(PyObject *tuple, Py_ssize_t pos);

/* Error handling */
PyObject *py_helper_err_occurred(void);
void py_helper_err_print(void);
void py_helper_err_set_string(PyObject *type, const char *message);
PyObject *py_helper_exc_oserror(void);
PyObject *py_helper_exc_runtime_error(void);
PyObject *py_helper_err_no_memory(void);
void py_helper_err_set_from_errno(int err);

/* Bytes helpers */
PyObject *py_helper_bytes_from_string_and_size(const char *v, Py_ssize_t len);
char *py_helper_bytes_as_string(PyObject *bytes);
Py_ssize_t py_helper_bytes_get_size(PyObject *bytes);

/* Long helpers */
long py_helper_long_as_long(PyObject *obj);

/* Callable helpers */
PyObject *py_helper_call_no_args(PyObject *callable);

/* Boolean helpers (Py_True / Py_False are macros) */
PyObject *py_helper_true(void);
PyObject *py_helper_false(void);

/* Additional exception type accessors */
PyObject *py_helper_exc_value_error(void);

/* GIL management (for releasing GIL during blocking I/O) */
typedef struct { void *state; } PyHelperThreadState;
PyHelperThreadState py_helper_save_thread(void);
void py_helper_restore_thread(PyHelperThreadState state);

/* Type infrastructure — Py_TYPE() is a macro, tp_free is a function pointer */
void py_helper_type_free(PyObject *self);

/* Exception type accessor — PyExc_TypeError is a macro-defined global */
PyObject *py_helper_exc_type_error(void);

/* Type flag constants (macros in CPython) */
unsigned long py_helper_tpflags_default(void);

/* ---------------------------------------------------------------------------
 * LazyRequestObject — C struct for layout agreement between C and Zig.
 * PyObject_HEAD is a macro, so the struct must be defined in C.
 * ---------------------------------------------------------------------------
 */
typedef struct {
    PyObject_HEAD

    /* Eagerly set (always valid, owned Python objects) */
    PyObject *method;          /* str */
    PyObject *path;            /* str (path only, no query) */
    PyObject *full_path;       /* str (original with query) */
    int keep_alive;            /* C bool */

    /* Raw pointers into Zig memory (valid while valid==1) */
    const char *raw_body_ptr;
    Py_ssize_t  raw_body_len;
    void       *raw_headers_ptr;   /* Header* in Zig, opaque in C */
    Py_ssize_t  raw_headers_count;
    const char *raw_qs_ptr;        /* query string after '?' */
    Py_ssize_t  raw_qs_len;

    /* Lazily cached Python objects (NULL until first access) */
    PyObject *cached_body;         /* bytes */
    PyObject *cached_headers;      /* dict[str, str], keys lowercased, dupes comma-joined */
    PyObject *cached_query_params; /* dict[str, str] */
    PyObject *cached_params;       /* dict[str, str] */

    /* Validity flag */
    int valid;  /* 1 while handler runs, 0 after _invalidate() */
} LazyRequestObject;

#endif /* PY_HELPERS_H */
