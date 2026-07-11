pub struct Ticket { pub status: u32 }
fn main() {
    let v = Ticket { status: 1 };
    println!("{}", v.state);
}
