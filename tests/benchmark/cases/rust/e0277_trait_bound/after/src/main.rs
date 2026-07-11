use std::fmt;

struct Custom { value: i32 }

impl fmt::Display for Custom {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "Custom({})", self.value)
    }
}

fn main() {
    let c = Custom { value: 42 };
    println!("{}", c);
}
