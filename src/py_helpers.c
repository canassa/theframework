/*
 * C implementations of helpers declared in py_helpers.h.
 * These wrap Python/greenlet C API macros that Zig's @cImport cannot translate.
 */
#include "py_helpers.h"
#include "greenlet.h"

/* --- Python constants --- */

PyObject *py_helper_none(void) {
    return Py_None;
}

void py_helper_incref(PyObject *o) {
    Py_INCREF(o);
}

void py_helper_decref(PyObject *o) {
    Py_DECREF(o);
}

int py_helper_meth_noargs(void) {
    return METH_NOARGS;
}

int py_helper_meth_varargs(void) {
    return METH_VARARGS;
}

PyMethodDef py_helper_method_sentinel(void) {
    PyMethodDef sentinel = {NULL, NULL, 0, NULL};
    return sentinel;
}

PyObject *py_helper_module_create(PyModuleDef *def) {
    return PyModule_Create(def);
}

PyModuleDef_Base py_helper_module_def_head_init(void) {
    PyModuleDef_Base base = PyModuleDef_HEAD_INIT;
    return base;
}

/* --- Greenlet C API --- */

int py_helper_greenlet_import(void) {
    PyGreenlet_Import();
    if (_PyGreenlet_API == NULL) {
        return -1;
    }
    return 0;
}

PyObject *py_helper_greenlet_getcurrent(void) {
    return (PyObject *)PyGreenlet_GetCurrent();
}

PyObject *py_helper_greenlet_new(PyObject *run, PyObject *parent) {
    return (PyObject *)PyGreenlet_New(run, (PyGreenlet *)parent);
}

PyObject *py_helper_greenlet_switch(PyObject *greenlet, PyObject *args, PyObject *kwargs) {
    return PyGreenlet_Switch((PyGreenlet *)greenlet, args, kwargs);
}

int py_helper_greenlet_active(PyObject *greenlet) {
    return PyGreenlet_ACTIVE((PyGreenlet *)greenlet);
}

int py_helper_greenlet_started(PyObject *greenlet) {
    return PyGreenlet_STARTED((PyGreenlet *)greenlet);
}

int py_helper_greenlet_main(PyObject *greenlet) {
    return PyGreenlet_MAIN((PyGreenlet *)greenlet);
}

/* --- Tuple helpers --- */

PyObject *py_helper_tuple_new(Py_ssize_t size) {
    return PyTuple_New(size);
}

int py_helper_tuple_setitem(PyObject *tuple, Py_ssize_t pos, PyObject *item) {
    return PyTuple_SetItem(tuple, pos, item);
}

/* --- Error handling --- */

PyObject *py_helper_err_occurred(void) {
    return PyErr_Occurred();
}

void py_helper_err_print(void) {
    PyErr_Print();
}

void py_helper_err_set_string(PyObject *type, const char *message) {
    PyErr_SetString(type, message);
}

PyObject *py_helper_exc_oserror(void) {
    return PyExc_OSError;
}

PyObject *py_helper_exc_runtime_error(void) {
    return PyExc_RuntimeError;
}

PyObject *py_helper_err_no_memory(void) {
    return PyErr_NoMemory();
}

void py_helper_err_set_from_errno(int err) {
    errno = err;
    PyErr_SetFromErrno(PyExc_OSError);
}

/* --- Bytes helpers --- */

PyObject *py_helper_bytes_from_string_and_size(const char *v, Py_ssize_t len) {
    return PyBytes_FromStringAndSize(v, len);
}

char *py_helper_bytes_as_string(PyObject *bytes) {
    return PyBytes_AsString(bytes);
}

Py_ssize_t py_helper_bytes_get_size(PyObject *bytes) {
    return PyBytes_GET_SIZE(bytes);
}

/* --- Long helpers --- */

long py_helper_long_as_long(PyObject *obj) {
    return PyLong_AsLong(obj);
}

/* --- Callable helpers --- */

PyObject *py_helper_call_no_args(PyObject *callable) {
    return PyObject_CallNoArgs(callable);
}

/* --- Boolean helpers --- */

PyObject *py_helper_true(void) { return Py_True; }
PyObject *py_helper_false(void) { return Py_False; }

/* --- Additional exception types --- */

PyObject *py_helper_exc_value_error(void) { return PyExc_ValueError; }

/* --- GIL management --- */

PyHelperThreadState py_helper_save_thread(void) {
    PyHelperThreadState s;
    s.state = (void *)PyEval_SaveThread();
    return s;
}

void py_helper_restore_thread(PyHelperThreadState state) {
    PyEval_RestoreThread((PyThreadState *)state.state);
}
