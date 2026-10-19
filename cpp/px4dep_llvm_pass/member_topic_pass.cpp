// M6 targeted LLVM pass: bind EKF aid-source message instances to uORB topics.
//
// Scope (deliberately narrow, not a general framework):
//   * only calls to EKF2::PublishAidSourceStatus<...>
//   * only the EKF2 publication member and the Ekf aid-source accessor used by
//     that call site
//   * at -O0, where member GEP field indices and the ORB_ID metadata operand
//     are still explicit
//
// The pass emits JSON Lines.  It does not modify the module and does not
// create end-to-end paths; downstream Python code joins these IR bindings to
// AST writer evidence.
//
// Environment:
//   PXDEP_LLVM_OUTPUT  path of the JSONL file (required)
//
// Build with cpp/px4dep_llvm_pass/build.sh, then:
//   opt-14 -load-pass-plugin <plugin.so> -passes=px4dep-member-topic \
//          -disable-output <EKF2.ll>

#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/Demangle/Demangle.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/DebugInfoMetadata.h"
#include "llvm/IR/DerivedTypes.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/GetElementPtrTypeIterator.h"
#include "llvm/IR/GlobalVariable.h"
#include "llvm/IR/InstIterator.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/PassManager.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/raw_ostream.h"

#include <cstdlib>
#include <map>
#include <memory>
#include <string>
#include <vector>

using namespace llvm;

namespace {

constexpr const char *kPassName = "px4dep-member-topic";

struct TopicBinding {
  unsigned field_index = 0;
  std::string topic;
  std::string topic_global;
  std::string constructor_function;
  std::string constructor_file;
  unsigned constructor_line = 0;
  std::string constructor_instruction;
};

struct AccessorInfo {
  std::string qualified_name;
  std::string mangled_name;
  std::string message_type;
  unsigned member_index = 0;
  std::string return_instruction;
  bool resolved = false;
};

std::string demangle(const Function *fn) {
  if (!fn) {
    return "";
  }
  return llvm::demangle(fn->getName().str());
}

Function *calledFunction(CallBase *call) {
  if (!call) {
    return nullptr;
  }
  return call->getCalledFunction();
}

std::string printValue(Value *value) {
  if (!value) {
    return "";
  }
  std::string text;
  raw_string_ostream os(text);
  value->print(os);
  return os.str();
}

std::string fileName(const DIScope *scope, std::string &directory) {
  if (!scope) {
    return "";
  }
  directory = scope->getDirectory().str();
  return scope->getFilename().str();
}

struct SourceLoc {
  std::string file;
  unsigned line = 0;
  unsigned column = 0;
};

SourceLoc sourceLoc(const Instruction &inst) {
  SourceLoc loc;
  const DebugLoc &debug = inst.getDebugLoc();
  if (!debug) {
    return loc;
  }
  const DILocation *di = debug.get();
  loc.line = di->getLine();
  loc.column = di->getColumn();
  std::string directory;
  loc.file = fileName(di->getScope(), directory);
  if (!loc.file.empty() && loc.file[0] != '/') {
    SmallString<256> joined(directory);
    sys::path::append(joined, loc.file);
    loc.file = joined.str().str();
  }
  return loc;
}

// Return the constant field index of a GEP whose base is (a pointer to) the
// named struct type.  The GEP may have an arbitrary number of zero indices
// before the final non-zero field index.
bool structNameIs(StructType *type, StringRef expected) {
  if (!type->hasName()) {
    return false;
  }
  StringRef name = type->getStructName();
  if (name == expected) {
    return true;
  }
  if (name.consume_front("struct.") || name.consume_front("class.")) {
    return name == expected;
  }
  return false;
}

bool gepFieldIndex(Value *value, StringRef struct_name, unsigned &index,
                   GetElementPtrInst *&gep_out) {
  auto *gep = dyn_cast<GetElementPtrInst>(value->stripPointerCasts());
  if (!gep) {
    return false;
  }
  auto *base = gep->getPointerOperandType()->getPointerElementType();
  auto *struct_type = dyn_cast<StructType>(base);
  if (!struct_type) {
    return false;
  }
  if (!structNameIs(struct_type, struct_name)) {
    return false;
  }
  // Walk the GEP type iterator.  The first index is the pointer index; every
  // later constant index is a struct field index.  The last one is what we
  // need for a direct member access such as &EKF2::_pub.
  unsigned last = 0;
  bool have = false;
  for (auto it = gep_type_begin(gep), end = gep_type_end(gep); it != end; ++it) {
    if (auto *constant = dyn_cast<ConstantInt>(it.getOperand())) {
      last = static_cast<unsigned>(constant->getZExtValue());
      have = true;
    } else {
      return false;
    }
  }
  if (!have) {
    return false;
  }
  index = last;
  gep_out = gep;
  return true;
}

bool metadataTopic(Value *value, std::string &global_name) {
  Value *stripped = value->stripPointerCasts();
  if (auto *global = dyn_cast<GlobalVariable>(stripped)) {
    global_name = global->getName().str();
    return true;
  }
  if (auto *constant = dyn_cast<ConstantExpr>(value)) {
    if (constant->getOpcode() == Instruction::BitCast ||
        constant->getOpcode() == Instruction::GetElementPtr) {
      return metadataTopic(constant->getOperand(0), global_name);
    }
  }
  return false;
}

std::string stripOrbPrefix(StringRef name) {
  if (name.startswith("__orb_")) {
    return name.drop_front(6).str();
  }
  if (name.startswith("_orb_")) {
    return name.drop_front(5).str();
  }
  return name.str();
}

class MemberTopicPass : public PassInfoMixin<MemberTopicPass> {
public:
  explicit MemberTopicPass(std::string output_path)
      : output_path_(std::move(output_path)) {}

  PreservedAnalyses run(Module &module, ModuleAnalysisManager &) {
    std::error_code error;
    raw_fd_ostream out(output_path_, error, sys::fs::OF_Text);
    if (error) {
      report_fatal_error("px4dep: cannot open " + StringRef(output_path_) +
                         ": " + error.message());
    }

    std::map<unsigned, TopicBinding> bindings;
    collectPublicationBindings(module, bindings);

    unsigned call_sites = 0;
    unsigned resolved = 0;
    unsigned unresolved = 0;
    unsigned candidate_constructors = 0;

    for (Function &function : module) {
      for (Instruction &inst : instructions(function)) {
        auto *call = dyn_cast<CallBase>(&inst);
        if (!call) {
          continue;
        }
        Function *callee = calledFunction(call);
        if (!callee) {
          continue;
        }
        const std::string callee_name = demangle(callee);
        if (callee_name.find("EKF2::PublishAidSourceStatus<") == std::string::npos) {
          continue;
        }
        ++call_sites;
        SourceLoc loc = sourceLoc(inst);
        AccessorInfo accessor;
        Value *publication_value = nullptr;
        unsigned publication_index = 0;
        GetElementPtrInst *publication_gep = nullptr;

        // The first argument is `this`; scan the rest for the two values the
        // pass understands instead of hard-coding the template signature.
        for (unsigned index = 1; index < call->arg_size(); ++index) {
          Value *arg = call->getArgOperand(index);
          AccessorInfo candidate = resolveAccessor(arg, module);
          if (candidate.resolved) {
            accessor = candidate;
            continue;
          }
          unsigned field = 0;
          GetElementPtrInst *gep = nullptr;
          if (gepFieldIndex(arg, "EKF2", field, gep)) {
            publication_value = arg;
            publication_index = field;
            publication_gep = gep;
          }
        }

        auto binding_it = bindings.find(publication_index);
        bool ok = accessor.resolved && publication_gep != nullptr &&
                  binding_it != bindings.end() &&
                  binding_it->second.topic != "";
        if (!ok) {
          ++unresolved;
          std::string reason = "publication_field_not_bound";
          if (!accessor.resolved) {
            reason = "status_argument_not_aid_source_accessor";
          } else if (!publication_gep) {
            reason = "publication_argument_not_ekf2_member";
          } else if (binding_it == bindings.end()) {
            reason = "no_orb_metadata_for_publication_field";
          }
          emitUnresolved(out, module, callee, loc, reason, call_sites);
          continue;
        }

        const TopicBinding &binding = binding_it->second;
        if (binding.topic != accessor.message_type &&
            !topicMatchesMessageType(binding.topic, accessor.message_type)) {
          ++unresolved;
          emitUnresolved(out, module, callee, loc,
                         "topic_message_type_mismatch", call_sites);
          continue;
        }

        ++resolved;
        emitBinding(out, module, callee, loc, accessor, binding,
                    publication_index, *call);
      }
    }

    // Count constructors once, for the metric; they are not call sites of
    // interest but show that the metadata map came from real IR.
    for (Function &function : module) {
      for (Instruction &inst : instructions(function)) {
        auto *call = dyn_cast<CallBase>(&inst);
        if (!call) {
          continue;
        }
        Function *callee = calledFunction(call);
        if (!callee) {
          continue;
        }
        if (demangle(callee).find("uORB::PublicationMulti<") != std::string::npos &&
            demangle(callee).find("::PublicationMulti(") != std::string::npos) {
          ++candidate_constructors;
        }
      }
    }

    llvm::json::OStream json(out);
    json.object([&] {
      json.attribute("kind", "llvm_pass_stats");
      json.attribute("pass", kPassName);
      json.attribute("module", module.getModuleIdentifier());
      json.attribute("publish_aid_source_calls", call_sites);
      json.attribute("bindings", resolved);
      json.attribute("unresolved", unresolved);
      json.attribute("publication_fields", static_cast<uint64_t>(bindings.size()));
      json.attribute("publication_constructors", candidate_constructors);
      json.attribute("interpretation",
                     "member_to_topic_binding_only_not_end_to_end");
      json.attribute("optimization", "O0_required_for_field_indices");
    });
    out << "\n";
    out.flush();
    return PreservedAnalyses::all();
  }

private:
  std::string output_path_;

  bool topicMatchesMessageType(StringRef topic, StringRef message_type) {
    // PX4 topics are snake_case forms of the message type; require a real
    // relationship (e.g. estimator_aid_source1d_s -> estimator_aid_src_*).
    if (topic.contains("aid_src") && message_type.contains("aid_source")) {
      return true;
    }
    return false;
  }

  void collectPublicationBindings(Module &module,
                                  std::map<unsigned, TopicBinding> &bindings) {
    for (Function &function : module) {
      for (Instruction &inst : instructions(function)) {
        auto *call = dyn_cast<CallBase>(&inst);
        if (!call) {
          continue;
        }
        Function *callee = calledFunction(call);
        if (!callee) {
          continue;
        }
        const std::string callee_name = demangle(callee);
        if (callee_name.find("uORB::PublicationMulti<") == std::string::npos ||
            callee_name.find("::PublicationMulti(") == std::string::npos) {
          continue;
        }
        if (call->arg_size() < 2) {
          continue;
        }
        unsigned field = 0;
        GetElementPtrInst *gep = nullptr;
        if (!gepFieldIndex(call->getArgOperand(0), "EKF2", field, gep)) {
          continue;
        }
        std::string global_name;
        if (!metadataTopic(call->getArgOperand(1), global_name)) {
          continue;
        }
        if (!StringRef(global_name).startswith("__orb_")) {
          continue;
        }
        TopicBinding binding;
        binding.field_index = field;
        binding.topic_global = global_name;
        binding.topic = stripOrbPrefix(global_name);
        binding.constructor_function = callee_name;
        SourceLoc loc = sourceLoc(inst);
        binding.constructor_file = loc.file;
        binding.constructor_line = loc.line;
        binding.constructor_instruction = printValue(call);
        bindings[field] = binding;
      }
    }
  }

  AccessorInfo resolveAccessor(Value *value, Module &module) {
    AccessorInfo info;
    auto *call = dyn_cast<CallBase>(value);
    if (!call) {
      return info;
    }
    Function *callee = calledFunction(call);
    if (!callee) {
      return info;
    }
    std::string name = demangle(callee);
    if (name.find("Ekf::aid_src_") == std::string::npos) {
      return info;
    }
    info.qualified_name = name;
    info.mangled_name = callee->getName().str();

    Function *definition = callee;
    if (definition->isDeclaration()) {
      // Inline accessors are emitted as linkonce_odr definitions in this TU.
      // If the called function is a declaration, there is no body to inspect.
      return info;
    }
    for (Instruction &inst : instructions(definition)) {
      auto *ret = dyn_cast<ReturnInst>(&inst);
      if (!ret) {
        continue;
      }
      Value *returned = ret->getReturnValue();
      if (!returned) {
        continue;
      }
      unsigned field = 0;
      GetElementPtrInst *gep = nullptr;
      if (!gepFieldIndex(returned, "Ekf", field, gep)) {
        continue;
      }
      info.member_index = field;
      info.return_instruction = printValue(returned);
      info.resolved = true;

      auto *return_type = definition->getReturnType();
      if (return_type->isPointerTy()) {
        auto *element = return_type->getPointerElementType();
        if (auto *structure = dyn_cast<StructType>(element)) {
          if (structure->hasName()) {
            StringRef name = structure->getStructName();
            name.consume_front("struct.");
            name.consume_front("class.");
            info.message_type = name.str();
          }
        }
      }
      return info;
    }
    return info;
  }

  void emitBinding(raw_ostream &out, Module &module,
                   Function *callee, const SourceLoc &loc,
                   const AccessorInfo &accessor, const TopicBinding &binding,
                   unsigned publication_index, const CallBase &call) {
    llvm::json::OStream json(out);
    json.object([&] {
      json.attribute("kind", "llvm_member_topic_binding");
      json.attribute("pass", kPassName);
      json.attribute("evidence_kind", "llvm_ir");
      json.attribute("module", module.getModuleIdentifier());
      json.attribute("publisher_function", demangle(callee));
      json.attribute("publisher_mangled", callee->getName().str());
      json.attribute("accessor_function", accessor.qualified_name);
      json.attribute("accessor_mangled", accessor.mangled_name);
      json.attribute("message_type", accessor.message_type);
      json.attribute("topic", binding.topic);
      json.attribute("topic_global", binding.topic_global);
      json.attribute("ekf_member_field_index", accessor.member_index);
      json.attribute("ekf2_publication_field_index", publication_index);
      json.attribute("accessor_return_instruction", accessor.return_instruction);
      json.attribute("publication_constructor_function",
                     binding.constructor_function);
      json.attribute("publication_constructor_file",
                     binding.constructor_file);
      json.attribute("publication_constructor_line",
                     binding.constructor_line);
      json.attribute("publication_constructor_instruction",
                     binding.constructor_instruction);
      json.attribute("call_site_file", loc.file);
      json.attribute("call_site_line", loc.line);
      json.attribute("call_site_column", loc.column);
      json.attribute("call_instruction", printValue(
                         const_cast<CallBase *>(&call)));
      json.attribute("confidence", "high");
      json.attribute("interpretation",
                     "message_instance_to_topic_binding_only");
      json.attribute("end_to_end_claim", false);
    });
    out << "\n";
  }

  void emitUnresolved(raw_ostream &out, Module &module,
                      Function *callee, const SourceLoc &loc,
                      StringRef reason, unsigned call_index) {
    llvm::json::OStream json(out);
    json.object([&] {
      json.attribute("kind", "llvm_unresolved");
      json.attribute("pass", kPassName);
      json.attribute("evidence_kind", "llvm_ir");
      json.attribute("module", module.getModuleIdentifier());
      json.attribute("publisher_function", demangle(callee));
      json.attribute("reason", reason);
      json.attribute("call_site_file", loc.file);
      json.attribute("call_site_line", loc.line);
      json.attribute("call_site_column", loc.column);
      json.attribute("call_index", call_index);
      json.attribute("confidence", "high");
      json.attribute("interpretation", "analysis_gap_not_unreachable");
    });
    out << "\n";
  }
};

} // namespace

extern "C" LLVM_ATTRIBUTE_WEAK ::llvm::PassPluginLibraryInfo
llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, kPassName, "0.1.0",
          [](PassBuilder &builder) {
            builder.registerPipelineParsingCallback(
                [](StringRef name, ModulePassManager &manager,
                   ArrayRef<PassBuilder::PipelineElement>) {
                  if (name != kPassName) {
                    return false;
                  }
                  const char *output = std::getenv("PXDEP_LLVM_OUTPUT");
                  if (!output || !*output) {
                    report_fatal_error(
                        "px4dep: PXDEP_LLVM_OUTPUT is not set");
                  }
                  manager.addPass(MemberTopicPass(output));
                  return true;
                });
          }};
}
